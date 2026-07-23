"""Auto-remove stuck/stalled tasks from the board.

A stage the upstream reports as ``stalled`` (its own liveness watchdog gave up —
TFactory's #95 sweep) is a dead task; the cockpit must drop it instead of showing
it as "running" forever, which just confuses users.
"""

from __future__ import annotations

import httpx
from cfactory.adapters import AIFactoryAdapter
from cfactory.models import Service, ServiceState
from cfactory.progress import LiveProgressHub, _is_dead_stage, poll_progress_once
from cfactory.store import WorkItemStore


def _store() -> WorkItemStore:
    return WorkItemStore("sqlite://")  # in-memory


# ── detector ───────────────────────────────────────────────────────────────


def test_is_dead_stage_matches_stalled_and_stuck():
    assert _is_dead_stage("stalled") is True
    assert _is_dead_stage("watchdog_stalled") is True
    assert _is_dead_stage("stuck") is True


def test_is_dead_stage_ignores_healthy_and_substrings():
    assert _is_dead_stage("in_progress") is False
    assert _is_dead_stage("coding") is False
    assert _is_dead_stage("done") is False
    assert _is_dead_stage("triaged") is False
    # token-boundary, never substring: "installed" must NOT read as stalled
    assert _is_dead_stage("installed") is False
    assert _is_dead_stage(None) is False


# ── store.prune_stuck ───────────────────────────────────────────────────────


def test_prune_stuck_deletes_matching_row():
    s = _store()
    s.upsert_snapshot("7", Service.TFACTORY, ServiceState(task_id="tf:7", status="stalled"))
    assert s.prune_stuck(Service.TFACTORY, {"tf:7"}) == 1
    assert s.get("7") is None


def test_prune_stuck_leaves_other_rows():
    s = _store()
    s.upsert_snapshot("7", Service.TFACTORY, ServiceState(task_id="tf:7", status="stalled"))
    s.upsert_snapshot("8", Service.TFACTORY, ServiceState(task_id="tf:8", status="triaged"))
    assert s.prune_stuck(Service.TFACTORY, {"tf:7"}) == 1
    assert s.get("7") is None
    assert s.get("8") is not None


def test_prune_stuck_empty_set_is_noop():
    s = _store()
    s.upsert_snapshot("7", Service.TFACTORY, ServiceState(task_id="tf:7", status="stalled"))
    assert s.prune_stuck(Service.TFACTORY, set()) == 0
    assert s.get("7") is not None


def test_prune_stuck_is_per_service():
    s = _store()
    s.upsert_snapshot("7", Service.TFACTORY, ServiceState(task_id="tf:7", status="stalled"))
    s.upsert_snapshot("7", Service.PFACTORY, ServiceState(task_id="pf:7", status="planning"))
    # Pruning by a tfactory task id must not touch a pfactory-keyed row.
    assert s.prune_stuck(Service.PFACTORY, {"tf:7"}) == 0
    assert s.get("7") is not None


# ── poll integration: stalled item auto-removed, healthy kept ───────────────


def _mock_ai(tasks: list[dict]) -> AIFactoryAdapter:
    return AIFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": tasks})),
    )


def test_poll_auto_removes_stalled_and_keeps_healthy():
    ai = _mock_ai(
        [
            {
                "id": "good",
                "status": "coding",
                "phase": "code",
                "metadata": {"githubIssueNumber": 1},
            },
            {
                "id": "dead",
                "status": "stalled",
                "phase": "watchdog_stalled",
                "metadata": {"githubIssueNumber": 2},
            },
        ]
    )
    hub = LiveProgressHub()
    store = _store()
    poll_progress_once(hub, [ai], store)

    assert store.get("1") is not None  # healthy task boarded
    assert store.get("2") is None  # stalled task never boarded (auto-removed)
    # and it is absent from the live progress feed too
    assert all(lp.correlation_key != "2" for lp in hub.snapshot())


def test_poll_deletes_a_task_that_later_stalls():
    hub = LiveProgressHub()
    store = _store()
    # First poll: healthy → on the board.
    poll_progress_once(
        hub,
        [
            _mock_ai(
                [
                    {
                        "id": "x",
                        "status": "coding",
                        "phase": "code",
                        "metadata": {"githubIssueNumber": 5},
                    }
                ]
            )
        ],
        store,
    )
    assert store.get("5") is not None
    # Next poll: the upstream now reports it stalled → the card is removed.
    poll_progress_once(
        hub,
        [
            _mock_ai(
                [
                    {
                        "id": "x",
                        "status": "stalled",
                        "phase": "watchdog_stalled",
                        "metadata": {"githubIssueNumber": 5},
                    }
                ]
            )
        ],
        store,
    )
    assert store.get("5") is None

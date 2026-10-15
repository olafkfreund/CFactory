"""Stalled/stuck tasks are kept on the board, stored as stalled (#479, #480).

A stage the upstream reports as ``stalled`` (its own liveness watchdog gave up —
TFactory's #95 sweep) is kept out of the live progress feed but stays in the store,
so history is kept and the stage reads ``stalled`` rather than the row vanishing.
The poller never deletes work-item rows.
"""

from __future__ import annotations

import time

import httpx
from cfactory import task_process
from cfactory.adapters import AIFactoryAdapter, TFactoryAdapter
from cfactory.models import Service, ServiceState, WorkItem
from cfactory.progress import LiveProgressHub, _is_dead_stage, poll_progress_once
from cfactory.store import WorkItemStore, compute_liveness


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


# ── poll integration: stalled item kept in the store, out of the hub ─────────


def _mock_ai(tasks: list[dict]) -> AIFactoryAdapter:
    return AIFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": tasks})),
    )


def test_poll_keeps_stalled_out_of_the_hub_but_in_the_store():
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
    kept = store.get("2")  # stalled task stays on the board, stored as stalled
    assert kept is not None
    assert kept.aifactory.status == "stalled"
    # but it is absent from the live progress feed
    assert all(lp.correlation_key != "2" for lp in hub.snapshot())


def test_poll_keeps_a_task_that_later_stalls():
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
    # Next poll: the upstream now reports it stalled → the card is kept, as stalled.
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
    kept = store.get("5")
    assert kept is not None
    assert kept.aifactory.status == "stalled"


# ── #479: a stalled downstream stage lights its plan node as stalled ────────────


def test_stalled_tfactory_stage_marks_plan_node_stalled():
    store = _store()
    store.upsert_snapshot("9", Service.AIFACTORY, ServiceState(task_id="ai:9", status="completed"))
    tf = TFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"tasks": [{"spec_id": "tf-9", "status": "stalled", "github_issue": 9}]},
            )
        ),
    )
    poll_progress_once(LiveProgressHub(), [tf], store)

    session = {
        "board_state": "done",
        "epic": {"children": [{"key": "a"}]},
        "emit_result": {"child_numbers": {"a": 9}},
    }
    graph = task_process._build_plan_graph(session, store)
    assert graph is not None
    assert graph["nodes"][0]["status"] == "stalled"


# ── #480: a silent running row keeps updated_at so liveness can flag it ─────────


def test_silent_running_row_is_kept_and_flagged_stalled(monkeypatch):
    item = [{"id": "x", "status": "in_progress", "metadata": {"githubIssueNumber": 1}}]
    hub = LiveProgressHub()
    store = _store()
    poll_progress_once(hub, [_mock_ai(item)], store)
    first = store.get("1")
    assert first is not None

    monkeypatch.setenv("CFACTORY_STALL_DEADLINE_SECONDS", "0.001")
    time.sleep(0.01)
    poll_progress_once(hub, [_mock_ai(item)], store)
    poll_progress_once(hub, [_mock_ai(item)], store)

    row = store.get("1")
    assert row is not None
    assert row.updated_at == first.updated_at  # no-op polls never bump it (#105)
    assert compute_liveness(row).stalled is True


def test_plan_child_stall_check_is_whole_token():
    # "installed" contains "stalled" as a substring; it must still read completed.
    wi = WorkItem(correlation_key="9", aifactory=ServiceState(status="deps_installed_done"))
    assert task_process._plan_child_state(wi)[0] == "completed"

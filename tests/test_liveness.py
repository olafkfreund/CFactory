"""Tests for the stuck-task / last-activity liveness signal (RFC-0008 §3.4, #105)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cfactory.models import ServiceState, WorkItem
from cfactory.store import compute_liveness, stall_deadline_seconds

_NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
_DEADLINE = 900.0


def _item(*, updated_at=None, **stages) -> WorkItem:
    kw = {svc: ServiceState(status=st) for svc, st in stages.items()}
    return WorkItem(correlation_key="42", updated_at=updated_at, **kw)


# ── compute_liveness ─────────────────────────────────────────────────────


def test_stuck_reviewing_is_stalled() -> None:
    item = _item(
        pfactory="emitted",
        aifactory="merged",
        tfactory="reviewing",  # non-terminal frontier
        updated_at=_NOW - timedelta(seconds=1800),
    )
    lv = compute_liveness(item, now=_NOW, deadline_seconds=_DEADLINE)
    assert lv.stalled is True
    assert lv.active_service == "tfactory" and lv.active_status == "reviewing"
    assert lv.last_activity_age_seconds == 1800.0


def test_fresh_active_task_is_not_stalled() -> None:
    item = _item(
        pfactory="emitted", aifactory="coding",
        updated_at=_NOW - timedelta(seconds=60),
    )
    lv = compute_liveness(item, now=_NOW, deadline_seconds=_DEADLINE)
    assert lv.active_service == "aifactory" and lv.stalled is False


def test_all_terminal_is_settled_not_active() -> None:
    item = _item(
        pfactory="emitted", aifactory="merged", tfactory="triaged",
        updated_at=_NOW - timedelta(days=2),
    )
    lv = compute_liveness(item, now=_NOW, deadline_seconds=_DEADLINE)
    # furthest-along stage is terminal → no active frontier, never stalled
    assert lv.active_service is None and lv.stalled is False


def test_between_stages_is_not_flagged() -> None:
    # pfactory handed off (terminal) but aifactory hasn't started — no agent
    # claims to be working, so this is not a stall (avoid false positives).
    item = _item(pfactory="emitted", updated_at=_NOW - timedelta(days=1))
    lv = compute_liveness(item, now=_NOW, deadline_seconds=_DEADLINE)
    assert lv.active_service is None and lv.stalled is False


def test_naive_updated_at_is_handled() -> None:
    naive = (_NOW - timedelta(seconds=2000)).replace(tzinfo=None)
    item = _item(tfactory="reviewing", updated_at=naive)
    lv = compute_liveness(item, now=_NOW, deadline_seconds=_DEADLINE)
    assert lv.stalled is True  # naive timestamp treated as UTC, no crash


def test_missing_updated_at_is_not_stalled() -> None:
    item = _item(tfactory="reviewing", updated_at=None)
    lv = compute_liveness(item, now=_NOW, deadline_seconds=_DEADLINE)
    assert lv.last_activity_age_seconds == 0.0 and lv.stalled is False


def test_deadline_env_override(monkeypatch) -> None:
    assert stall_deadline_seconds() == 900.0
    monkeypatch.setenv("CFACTORY_STALL_DEADLINE_SECONDS", "120")
    assert stall_deadline_seconds() == 120.0
    monkeypatch.setenv("CFACTORY_STALL_DEADLINE_SECONDS", "nonsense")
    assert stall_deadline_seconds() == 900.0  # falls back


# ── API surface ──────────────────────────────────────────────────────────


def _post(client, key, service, status):
    return client.post(
        "/api/events",
        json={
            "correlation_key": key, "service": service, "task_id": f"{service}-t",
            "status": status, "phase": service, "updated_at": "2026-06-18T12:00:00Z",
        },
    )


def test_workitems_api_includes_liveness_and_stalled_count(client) -> None:
    _post(client, "100", "tfactory", "reviewing")  # active but freshly written
    body = client.get("/api/workitems").json()
    assert "stalled_count" in body
    item = body["items"][0]
    assert "liveness" in item
    lv = item["liveness"]
    # freshly ingested → active frontier but not yet stalled
    assert lv["active_service"] == "tfactory"
    assert lv["stalled"] is False
    assert body["stalled_count"] == 0

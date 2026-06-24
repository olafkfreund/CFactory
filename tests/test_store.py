"""Unit tests for the WorkItem correlation store (#6)."""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory.models import CompletionEvent, Service
from cfactory.store import _apply_terminal_or_scalar, _attach_access_verification


def _event(service: Service, status: str, key: str = "42") -> CompletionEvent:
    return CompletionEvent(
        correlation_key=key,
        service=service,
        task_id=f"{service.value}-task",
        status=status,
        phase=service.value,
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
    )


def test_create_then_get(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned"))
    wi = store.get("42")
    assert wi is not None
    assert wi.pfactory.task_id == "pfactory-task"
    assert wi.pfactory.status == "planned"
    assert len(wi.timeline) == 1


def test_delete_removes_work_item(store):
    store.upsert_from_event(_event(Service.TFACTORY, "planner_failed", key="bench-1"))
    assert store.get("bench-1") is not None
    assert store.delete("bench-1") is True
    assert store.get("bench-1") is None
    # Idempotent: deleting an already-gone item is a no-op that reports False.
    assert store.delete("bench-1") is False


def test_delete_missing_work_item_returns_false(store):
    assert store.delete("never-existed") is False


def test_events_across_services_thread_one_workitem(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned"))
    store.upsert_from_event(_event(Service.AIFACTORY, "coding"))
    store.upsert_from_event(_event(Service.TFACTORY, "triaged"))

    wi = store.get("42")
    assert wi is not None
    # All three service slices populated on the same correlation key.
    assert wi.pfactory.status == "planned"
    assert wi.aifactory.status == "coding"
    assert wi.tfactory.status == "triaged"
    # Timeline accumulates every event in order.
    assert len(wi.timeline) == 3
    assert [e.service for e in wi.timeline] == [
        Service.PFACTORY,
        Service.AIFACTORY,
        Service.TFACTORY,
    ]


def test_latest_event_overwrites_service_slice(store):
    store.upsert_from_event(_event(Service.AIFACTORY, "coding"))
    store.upsert_from_event(_event(Service.AIFACTORY, "done"))
    wi = store.get("42")
    assert wi is not None
    assert wi.aifactory.status == "done"  # slice reflects the latest
    assert len(wi.timeline) == 2  # but history is preserved


def test_list_orders_and_separates_keys(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned", key="1"))
    store.upsert_from_event(_event(Service.PFACTORY, "planned", key="2"))
    items = store.list()
    assert {wi.correlation_key for wi in items} == {"1", "2"}


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


# --------------------------------------------------------------------------
# #116: upsert_from_event was extracted into 4 documented helpers. These pin
# the extracted helpers directly (the orchestrator behaviour is exercised by
# the wider suite — workers / progress / access / verification tests).
# --------------------------------------------------------------------------


def test_apply_terminal_or_scalar_builds_slice_and_prunes_progress():
    prev = {"worker_progress": {"w1": [{"ts": 1}]}, "workers": {"w1": {"worker_id": "w1"}}}
    # Non-terminal: progress series preserved.
    running = _apply_terminal_or_scalar(prev, _event(Service.AIFACTORY, "coding"))
    assert running["status"] == "coding"
    assert running["worker_progress"] == {"w1": [{"ts": 1}]}
    # Terminal: progress series pruned (finished tasks must not bloat the store).
    done = _apply_terminal_or_scalar(prev, _event(Service.AIFACTORY, "done"))
    assert done["status"] == "done"
    assert done["worker_progress"] == {}


def test_attach_access_and_verification_coexist():
    ev = CompletionEvent(
        correlation_key="42",
        service=Service.TFACTORY,
        task_id="t",
        status="passed",
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
        access={"val3": False, "reason": "no creds"},
        verification={"achieved_level": "VAL-2", "claim": "honest"},
    )
    out = _attach_access_verification({}, ev)
    # Both annotations land under extra together (the original wrote them in two
    # passes; the merge must not drop either).
    assert out["extra"]["access"] == {"val3": False, "reason": "no creds"}
    assert out["extra"]["verification"] == {"achieved_level": "VAL-2", "claim": "honest"}


def test_attach_access_verification_noop_when_absent():
    out = _attach_access_verification({"status": "coding"}, _event(Service.AIFACTORY, "coding"))
    assert "extra" not in out

"""Unit tests for the WorkItem correlation store (#6)."""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.models import CompletionEvent, Service


def _event(service: Service, status: str, key: str = "42") -> CompletionEvent:
    return CompletionEvent(
        correlation_key=key,
        service=service,
        task_id=f"{service.value}-task",
        status=status,
        phase=service.value,
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
    )


def test_create_then_get(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned"))
    wi = store.get("42")
    assert wi is not None
    assert wi.pfactory.task_id == "pfactory-task"
    assert wi.pfactory.status == "planned"
    assert len(wi.timeline) == 1


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
    assert [e.service for e in wi.timeline] == [Service.PFACTORY, Service.AIFACTORY, Service.TFACTORY]


def test_latest_event_overwrites_service_slice(store):
    store.upsert_from_event(_event(Service.AIFACTORY, "coding"))
    store.upsert_from_event(_event(Service.AIFACTORY, "done"))
    wi = store.get("42")
    assert wi is not None
    assert wi.aifactory.status == "done"   # slice reflects the latest
    assert len(wi.timeline) == 2           # but history is preserved


def test_list_orders_and_separates_keys(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned", key="1"))
    store.upsert_from_event(_event(Service.PFACTORY, "planned", key="2"))
    items = store.list()
    assert {wi.correlation_key for wi in items} == {"1", "2"}


def test_get_missing_returns_none(store):
    assert store.get("nope") is None

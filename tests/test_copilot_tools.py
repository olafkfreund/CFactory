"""Tests for copilot read tools (#14) + their API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.copilot.tools import query_work_items, rollups, summarize_timeline
from cfactory.models import CompletionEvent, Service, Stage


def _ev(store, key, service, status, minute=0):
    store.upsert_from_event(CompletionEvent(
        correlation_key=key, service=service, task_id="t", status=status, phase=service.value,
        updated_at=datetime(2026, 6, 4, 12, minute, tzinfo=timezone.utc)))


def test_query_by_stage_and_status(store):
    _ev(store, "1", Service.PFACTORY, "human_review")
    _ev(store, "2", Service.AIFACTORY, "coding")
    assert {wi.correlation_key for wi in query_work_items(store, stage=Stage.PLAN)} == {"1"}
    assert {wi.correlation_key for wi in query_work_items(store, stage=Stage.CODE)} == {"2"}
    assert {wi.correlation_key for wi in query_work_items(store, status="coding")} == {"2"}


def test_summarize_timeline_orders_and_spans(store):
    _ev(store, "42", Service.PFACTORY, "human_review", minute=0)
    _ev(store, "42", Service.TFACTORY, "triaged", minute=5)
    summary = summarize_timeline(store, "42")
    assert summary is not None
    assert summary["event_count"] == 2
    assert summary["span_seconds"] == 300.0
    assert [e["service"] for e in summary["events"]] == ["pfactory", "tfactory"]
    assert summarize_timeline(store, "nope") is None


def test_rollups(store):
    _ev(store, "1", Service.PFACTORY, "human_review", minute=0)
    _ev(store, "1", Service.AIFACTORY, "coding", minute=2)
    _ev(store, "2", Service.TFACTORY, "triaged")
    r = rollups(store)
    assert r["total_work_items"] == 2
    assert r["by_stage"] == {"plan": 1, "code": 1, "test": 1}
    assert r["total_events"] == 3
    assert r["latency"]["max_seconds"] == 120.0
    assert r["cost"] is None


def test_rollups_and_timeline_endpoints(client, store):
    _ev(store, "7", Service.PFACTORY, "human_review", minute=0)
    _ev(store, "7", Service.TFACTORY, "triaged", minute=1)

    r = client.get("/api/rollups").json()
    assert r["total_work_items"] == 1 and r["by_stage"]["test"] == 1

    tl = client.get("/api/workitems/7/timeline").json()
    assert tl["event_count"] == 2 and tl["span_seconds"] == 60.0

    assert client.get("/api/workitems/nope/timeline").status_code == 404

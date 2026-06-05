"""Tests for the token spine (RFC-0001 v1.1 usage block + aggregation)."""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.copilot.tools import token_totals
from cfactory.models import CompletionEvent, Service, TokenUsage


def _ev(store, key, service, status, usage=None):
    store.upsert_from_event(CompletionEvent(
        correlation_key=key, service=service, task_id=f"{service.value}-t", status=status,
        phase=service.value, updated_at=datetime.now(timezone.utc), usage=usage))


def _u(i, o, cost):
    return TokenUsage(input_tokens=i, output_tokens=o, total_tokens=i + o, cost_usd=cost)


def test_usage_persists_on_slice_and_timeline(store):
    _ev(store, "42", Service.AIFACTORY, "done", _u(100, 50, 0.25))
    wi = store.get("42")
    assert wi is not None and wi.aifactory.usage is not None
    assert wi.aifactory.usage.total_tokens == 150
    assert wi.aifactory.usage.cost_usd == 0.25
    assert wi.timeline[-1].usage is not None  # rides the event too


def test_token_totals_aggregates_and_flags_instrumented(store):
    _ev(store, "1", Service.AIFACTORY, "done", _u(100, 50, 0.20))
    _ev(store, "2", Service.AIFACTORY, "done", _u(200, 100, 0.40))
    _ev(store, "1", Service.PFACTORY, "emitted")  # no usage → not instrumented

    t = token_totals(store)
    assert t["total"]["total_tokens"] == 450
    assert round(t["total"]["cost_usd"], 2) == 0.60
    assert t["by_service"]["aifactory"]["instrumented"] is True
    assert t["by_service"]["pfactory"]["instrumented"] is False
    assert {w["correlation_key"] for w in t["by_work_item"]} == {"1", "2"}
    # ranked by total_tokens desc
    assert t["by_work_item"][0]["correlation_key"] == "2"


def test_usage_not_double_counted_on_duplicate(store):
    _ev(store, "1", Service.AIFACTORY, "done", _u(100, 50, 0.20))
    _ev(store, "1", Service.AIFACTORY, "done", _u(100, 50, 0.20))  # idempotent dup
    assert token_totals(store)["total"]["total_tokens"] == 150


def test_tokens_endpoint(client):
    client.post("/api/events", json={
        "correlation_key": "7", "service": "aifactory", "task_id": "t", "status": "done",
        "updated_at": "2026-06-05T12:00:00Z",
        "usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100, "cost_usd": 0.10},
    })
    body = client.get("/api/tokens").json()
    assert body["total"]["total_tokens"] == 100
    assert body["by_service"]["aifactory"]["instrumented"] is True
    assert body["by_service"]["tfactory"]["instrumented"] is False

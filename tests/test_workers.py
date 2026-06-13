"""Tests for the per-worker / per-provider spine (RFC-0001 v1.3 additive).

Covers: a live ``phase:"worker"`` sub-event populating the slice's ``workers``
map without touching the scalar usage; idempotent re-emit (no double count); a
terminal event carrying ``usage.by_provider``/``by_model`` stored + rolled up;
and OLD events (no worker fields) ingesting unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.copilot.tools import token_by_worker, token_totals
from cfactory.models import CompletionEvent


def _ev(store, **kw):
    return store.upsert_from_event(
        CompletionEvent(updated_at=datetime.now(timezone.utc), **kw)
    )


def _worker(wid, provider, model, i, o, cost, dur=1000):
    return {
        "worker_id": wid, "subtask_id": f"st-{wid}", "agent_phase": "implement",
        "provider": provider, "model": model,
        "input_tokens": i, "output_tokens": o, "total_tokens": i + o,
        "cost_usd": cost, "duration_ms": dur,
    }


def test_worker_subevent_populates_workers_leaves_usage_untouched(store):
    wi, applied = _ev(
        store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w1", "claude", "opus", 100, 50, 0.30),
    )
    assert applied is True
    assert "w1" in wi.aifactory.workers
    assert wi.aifactory.workers["w1"].provider == "claude"
    # the service-level scalar usage slice is NOT set by a worker event
    assert wi.aifactory.usage is None
    # rollups recomputed from the worker map
    assert wi.aifactory.by_provider["claude"]["total_tokens"] == 150
    assert wi.aifactory.by_model["opus"]["total_tokens"] == 150


def test_worker_reemit_is_idempotent_no_double_count(store):
    _ev(store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w1", "claude", "opus", 100, 50, 0.30))
    wi, applied = _ev(
        store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w1", "claude", "opus", 100, 50, 0.30),
    )
    assert applied is False  # duplicate worker_id → timeline no-op
    assert wi.aifactory.by_provider["claude"]["total_tokens"] == 150  # not 300
    assert wi.aifactory.by_provider["claude"]["workers"] == 1
    # exactly one worker sub-event in the timeline
    worker_events = [e for e in wi.timeline if e.phase == "worker"]
    assert len(worker_events) == 1


def test_two_workers_two_providers_roll_up(store):
    _ev(store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w1", "claude", "opus", 100, 50, 0.30))
    wi, _ = _ev(
        store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w2", "ollama", "qwen", 80, 20, 0.0),
    )
    assert set(wi.aifactory.workers) == {"w1", "w2"}
    assert set(wi.aifactory.by_provider) == {"claude", "ollama"}
    assert wi.aifactory.by_provider["ollama"]["cost_usd"] == 0.0  # local provider
    assert wi.aifactory.by_provider["ollama"]["total_tokens"] == 100


def test_terminal_event_with_breakdowns_stored_and_preserves_workers(store):
    # live workers first
    _ev(store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w1", "claude", "opus", 100, 50, 0.30))
    # terminal event carries scalar usage + explicit breakdowns
    wi, _ = _ev(
        store, correlation_key="1", service="aifactory", task_id="t",
        status="done", phase="code",
        usage={
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "cost_usd": 0.30,
            "by_provider": {"claude": {"total_tokens": 150, "cost_usd": 0.30, "workers": 1}},
            "by_model": {"opus": {"total_tokens": 150, "cost_usd": 0.30, "workers": 1}},
        },
    )
    # scalar usage slice now set (terminal), back-compat field intact
    assert wi.aifactory.usage.total_tokens == 150
    assert wi.aifactory.status == "done"
    # breakdowns stored from the terminal event
    assert wi.aifactory.by_provider["claude"]["total_tokens"] == 150
    assert wi.aifactory.by_model["opus"]["total_tokens"] == 150
    # the live worker map is preserved across the terminal event
    assert "w1" in wi.aifactory.workers


def test_old_event_without_worker_fields_ingests_fine(store):
    wi, applied = _ev(
        store, correlation_key="9", service="pfactory", task_id="p",
        status="emitted", phase="plan",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": 0.01},
    )
    assert applied is True
    assert wi.pfactory.usage.total_tokens == 15  # legacy usage still works
    assert wi.pfactory.workers == {}             # empty per-worker map
    assert wi.pfactory.by_provider == {}
    # token_totals (back-compat aggregate) still sees it
    t = token_totals(store)
    assert t["by_service"]["pfactory"]["instrumented"] is True


def test_by_worker_api_aggregates_across_items(store):
    _ev(store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w1", "claude", "opus", 100, 50, 0.30))
    _ev(store, correlation_key="1", service="aifactory", task_id="t",
        status="worker_done", phase="worker",
        worker=_worker("w2", "ollama", "qwen", 80, 20, 0.0))
    out = token_by_worker(store)
    assert set(out["by_provider"]) == {"claude", "ollama"}
    assert out["by_provider"]["claude"]["total_tokens"] == 150
    assert len(out["by_work_item"]) == 1
    assert len(out["by_work_item"][0]["workers"]) == 2


def test_by_worker_endpoint(client):
    client.post("/api/events", json={
        "correlation_key": "7", "service": "aifactory", "task_id": "t",
        "status": "worker_done", "phase": "worker",
        "updated_at": "2026-06-13T12:00:00Z",
        "worker": {
            "worker_id": "wA", "subtask_id": "s1", "agent_phase": "implement",
            "provider": "claude", "model": "opus",
            "input_tokens": 80, "output_tokens": 20, "total_tokens": 100,
            "cost_usd": 0.10, "duration_ms": 800,
        },
    })
    body = client.get("/api/tokens/by_worker").json()
    assert body["by_provider"]["claude"]["total_tokens"] == 100
    assert body["by_work_item"][0]["workers"][0]["worker_id"] == "wA"
    # /api/tokens stays back-compat: a worker-only event leaves the scalar
    # service usage untouched (no by_service instrumentation from it).
    tok = client.get("/api/tokens").json()
    assert tok["by_service"]["aifactory"]["instrumented"] is False


def test_budget_exceeded_stored_and_surfaced(store):
    # Terminal event carries a soft budget that was exceeded (v1.3 P2).
    wi, applied = _ev(
        store, correlation_key="1", service="aifactory", task_id="t",
        status="done", phase="code",
        usage={
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "cost_usd": 7.50,
            "budget": {"limit_usd": 5.0, "spent_usd": 7.50, "exceeded": True},
        },
    )
    assert applied is True
    # stored on the slice's usage block
    assert wi.aifactory.usage.budget is not None
    assert wi.aifactory.usage.budget.exceeded is True
    assert wi.aifactory.usage.budget.limit_usd == 5.0
    assert wi.aifactory.usage.budget.spent_usd == 7.50
    # surfaced on the tokens API row for the cockpit badge
    t = token_totals(store)
    row = next(r for r in t["by_work_item"] if r["correlation_key"] == "1")
    assert row["budget"]["exceeded"] is True
    assert row["budget"]["limit_usd"] == 5.0


def test_event_without_budget_unchanged_no_budget_key(store):
    # The common case: usage with NO budget block → behaves exactly as before,
    # and the API row carries no `budget` key at all (back-compat).
    _ev(store, correlation_key="2", service="aifactory", task_id="t",
        status="done", phase="code",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": 0.01})
    wi = store.get("2")
    assert wi.aifactory.usage.budget is None
    t = token_totals(store)
    row = next(r for r in t["by_work_item"] if r["correlation_key"] == "2")
    assert "budget" not in row


def test_budget_endpoint_round_trip(client):
    client.post("/api/events", json={
        "correlation_key": "8", "service": "aifactory", "task_id": "t",
        "status": "done", "phase": "code", "updated_at": "2026-06-13T12:00:00Z",
        "usage": {
            "input_tokens": 80, "output_tokens": 20, "total_tokens": 100,
            "cost_usd": 12.0,
            "budget": {"limit_usd": 10.0, "spent_usd": 12.0, "exceeded": True},
        },
    })
    body = client.get("/api/tokens").json()
    row = next(r for r in body["by_work_item"] if r["correlation_key"] == "8")
    assert row["budget"]["exceeded"] is True
    assert row["budget"]["spent_usd"] == 12.0


def test_old_event_endpoint_still_ingests(client):
    resp = client.post("/api/events", json={
        "correlation_key": "11", "service": "tfactory", "task_id": "t",
        "status": "triaged", "updated_at": "2026-06-13T12:00:00Z",
        "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10, "cost_usd": 0.0},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

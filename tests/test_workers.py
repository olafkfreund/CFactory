"""Tests for the per-worker / per-provider spine (RFC-0001 v1.3 additive).

Covers: a live ``phase:"worker"`` sub-event populating the slice's ``workers``
map without touching the scalar usage; idempotent re-emit (no double count); a
terminal event carrying ``usage.by_provider``/``by_model`` stored + rolled up;
and OLD events (no worker fields) ingesting unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.copilot.tools import token_by_worker, token_totals, worker_progress
from cfactory.models import CompletionEvent
from cfactory.store import _PROGRESS_SERIES_CAP


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


# --- Tier 1.5: per-worker progress heartbeat series -----------------------

def _progress(store, *, ts, wid="w1", total_tokens=100, cost_usd=0.10, elapsed_ms=1000,
              correlation_key="1", status="running"):
    """Ingest a ``phase:"worker_progress"`` heartbeat at an explicit timestamp."""
    return store.upsert_from_event(CompletionEvent(
        correlation_key=correlation_key, service="aifactory", task_id="t",
        status=status, phase="worker_progress",
        updated_at=datetime.fromtimestamp(ts, tz=timezone.utc),
        worker={
            "worker_id": wid, "subtask_id": f"st-{wid}", "agent_phase": "implement",
            "provider": "claude", "model": "opus",
            "total_tokens": total_tokens, "cost_usd": cost_usd, "elapsed_ms": elapsed_ms,
        },
    ))


def test_progress_events_append_series(store):
    for i, (tok, cost) in enumerate([(100, 0.10), (200, 0.20), (300, 0.30)], start=1):
        wi, applied = _progress(store, ts=1_000_000 + i * 10, total_tokens=tok, cost_usd=cost)
        assert applied is True
    series = wi.aifactory.worker_progress["w1"]
    assert len(series) == 3
    assert [p.total_tokens for p in series] == [100, 200, 300]
    assert [p.cost_usd for p in series] == [0.10, 0.20, 0.30]
    # a heartbeat must NOT touch the scalar service slice (it's a stream)
    assert wi.aifactory.status is None
    assert wi.aifactory.phase is None
    assert wi.aifactory.usage is None
    assert wi.aifactory.workers == {}
    # heartbeats are not written to the timeline (high-frequency stream)
    assert all(e.phase != "worker_progress" for e in wi.timeline)


def test_progress_series_capped(store):
    over = _PROGRESS_SERIES_CAP + 25
    for i in range(over):
        wi, _ = _progress(store, ts=1_000_000 + i, total_tokens=i)
    series = wi.aifactory.worker_progress["w1"]
    assert len(series) == _PROGRESS_SERIES_CAP  # capped
    # the OLDEST points were dropped (keep last N), so the tail is the newest
    assert series[-1].total_tokens == over - 1
    assert series[0].total_tokens == over - _PROGRESS_SERIES_CAP


def test_terminal_event_prunes_progress_series(store):
    _progress(store, ts=1_000_001, total_tokens=100)
    _progress(store, ts=1_000_002, total_tokens=200)
    assert len(store.get("1").aifactory.worker_progress["w1"]) == 2
    # terminal event for the task → series pruned (only needed while running)
    _ev(store, correlation_key="1", service="aifactory", task_id="t",
        status="done", phase="code",
        usage={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200, "cost_usd": 0.20})
    wi = store.get("1")
    assert wi.aifactory.worker_progress == {}   # pruned
    assert wi.aifactory.status == "done"        # terminal slice intact
    assert wi.aifactory.usage.total_tokens == 200


def test_non_terminal_event_preserves_progress_series(store):
    _progress(store, ts=1_000_001, total_tokens=100)
    # a non-terminal ordinary event (e.g. running/code) must NOT wipe the series
    _ev(store, correlation_key="1", service="aifactory", task_id="t",
        status="running", phase="code")
    wi = store.get("1")
    assert "w1" in wi.aifactory.worker_progress
    assert wi.aifactory.status == "running"


def test_old_event_without_progress_unaffected(store):
    # legacy event with no worker_progress → empty series, ingests fine
    wi, applied = _ev(store, correlation_key="9", service="pfactory", task_id="p",
                      status="emitted", phase="plan",
                      usage={"input_tokens": 10, "output_tokens": 5,
                             "total_tokens": 15, "cost_usd": 0.01})
    assert applied is True
    assert wi.pfactory.worker_progress == {}


def test_worker_progress_dense_series_across_workers(store):
    # two workers heartbeat at interleaved timestamps → dense cumulative series
    _progress(store, ts=1_000_010, wid="w1", total_tokens=100, cost_usd=0.10)
    _progress(store, ts=1_000_020, wid="w2", total_tokens=50, cost_usd=0.05)
    _progress(store, ts=1_000_030, wid="w1", total_tokens=300, cost_usd=0.30)
    out = worker_progress(store, "1")
    # ts is coerced to epoch-MS from the event's updated_at (seconds * 1000).
    assert [p["ts"] for p in out["series"]] == [1_000_010_000, 1_000_020_000, 1_000_030_000]
    # cumulative-across-workers, carry-forward each worker's last sample
    assert out["series"][0]["total_tokens"] == 100          # w1 only
    assert out["series"][1]["total_tokens"] == 150          # w1=100 carried + w2=50
    assert out["series"][2]["total_tokens"] == 350          # w1=300 + w2=50
    assert abs(out["series"][2]["cost_usd"] - 0.35) < 1e-9
    assert set(out["workers"]) == {"w1", "w2"}


def test_worker_progress_unknown_task_empty(store):
    out = worker_progress(store, "does-not-exist")
    assert out["series"] == []
    assert out["workers"] == {}


def test_worker_progress_endpoint(client):
    for i, tok in enumerate([100, 200, 300], start=1):
        client.post("/api/events", json={
            "correlation_key": "42", "service": "aifactory", "task_id": "t",
            "status": "running", "phase": "worker_progress",
            "updated_at": f"2026-06-13T12:00:0{i}Z",
            "worker": {
                "worker_id": "wA", "subtask_id": "s1", "agent_phase": "implement",
                "provider": "claude", "model": "opus",
                "total_tokens": tok, "cost_usd": tok / 1000.0, "elapsed_ms": i * 1000,
            },
        })
    body = client.get("/api/tasks/42/worker-progress").json()
    assert len(body["series"]) == 3
    assert [p["total_tokens"] for p in body["series"]] == [100, 200, 300]
    assert "wA" in body["workers"]
    # existing endpoints unaffected: a worker_progress event leaves the scalar
    # service usage untouched (back-compat).
    tok_body = client.get("/api/tokens").json()
    assert tok_body["by_service"]["aifactory"]["instrumented"] is False
    bw = client.get("/api/tokens/by_worker").json()
    assert bw["by_work_item"] == []  # progress heartbeats are not terminal workers


def test_old_event_endpoint_still_ingests(client):
    resp = client.post("/api/events", json={
        "correlation_key": "11", "service": "tfactory", "task_id": "t",
        "status": "triaged", "updated_at": "2026-06-13T12:00:00Z",
        "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10, "cost_usd": 0.0},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

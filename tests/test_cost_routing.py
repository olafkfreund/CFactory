"""RFC-0014 (#124): CFactory surfaces the cost-aware routing decision next to
actual rolled-up spend.

Covers: the ``routing`` block lands on the plan slice + round-trips in the
timeline; the ``cost_routing`` tool / ``/api/tasks/{key}/cost-routing`` endpoint
join the pre-execution estimate with actual per-model spend (reusing the
billing-mode rule — real ``$`` only for metered work); estimate-vs-actual
variance; and back-compat (a task with no routing block still returns actuals,
legacy tasks ingest unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory.copilot.tools import cost_routing
from cfactory.models import CompletionEvent, Service, TokenUsage, WorkerUsage

_ROUTING = {
    "routing_class": "standard",
    "cost_estimate_usd": 2.10,
    "cost_ceiling_usd": 5.00,
    "budget_mode": "observe",
    "runtime": "claude",
    "policy": "operator-default",
    "autonomy": "review",
    "autonomy_reason": "difficulty medium",
    "rationale": "tier=medium floor=sonnet; planning kept at opus; qa/test downgraded",
    "phase_models": {
        "planning": "opus",
        "coding": "sonnet",
        "qa": "haiku",
        "test_gen": "ollama:qwen",
    },
}


def _plan_event(*, routing=None, key="42"):
    return CompletionEvent(
        correlation_key=key,
        service=Service.PFACTORY,
        task_id="p-1",
        status="planned",
        phase="plan",
        updated_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
        routing=routing,
    )


def _code_terminal_event(*, key="42"):
    """A metered AIFactory terminal event with a per-model/per-provider breakdown."""
    return CompletionEvent(
        correlation_key=key,
        service=Service.AIFACTORY,
        task_id="c-1",
        status="completed",
        phase="code",
        updated_at=datetime(2026, 6, 20, 12, 5, tzinfo=UTC),
        usage=TokenUsage(
            total_tokens=120_000,
            cost_usd=2.40,
            workers=[
                WorkerUsage(
                    worker_id="w1",
                    provider="claude",
                    model="sonnet",
                    total_tokens=120_000,
                    cost_usd=2.40,
                    billing_mode="api",
                )
            ],
            by_provider={
                "claude": {"total_tokens": 120_000, "cost_usd": 2.40, "billing_mode": "api"}
            },
            by_model={"sonnet": {"total_tokens": 120_000, "cost_usd": 2.40, "workers": 1}},
        ),
    )


# --- ingest / round-trip --------------------------------------------------


def test_routing_lands_on_the_plan_slice(store):
    item, _ = store.upsert_from_event(_plan_event(routing=_ROUTING))
    block = item.pfactory.extra["routing"]
    assert block["routing_class"] == "standard"
    assert block["cost_estimate_usd"] == 2.10
    assert block["phase_models"]["planning"] == "opus"
    assert block["autonomy"] == "review"


def test_routing_preserved_in_timeline(store):
    item, _ = store.upsert_from_event(_plan_event(routing=_ROUTING))
    ev = item.timeline[-1]
    assert ev.routing is not None
    assert ev.routing.cost_estimate_usd == 2.10
    assert ev.routing.budget_mode == "observe"


def test_no_routing_leaves_slice_clean(store):
    item, _ = store.upsert_from_event(_plan_event(routing=None))
    assert "routing" not in (item.pfactory.extra or {})


# --- cost_routing join: estimate vs actual --------------------------------


def test_cost_routing_joins_estimate_with_actual_spend(store):
    store.upsert_from_event(_plan_event(routing=_ROUTING))
    store.upsert_from_event(_code_terminal_event())

    result = cost_routing(store, "42")
    assert result is not None
    assert result["routing"]["routing_class"] == "standard"
    # Actual metered spend rolled up from the terminal usage breakdown.
    assert result["actual_cost_usd"] == 2.40
    assert result["actual_by_model"]["sonnet"]["cost_usd"] == 2.40
    assert result["actual_total_tokens"] == 120_000

    eva = result["estimate_vs_actual"]
    assert eva["estimate_usd"] == 2.10
    assert eva["ceiling_usd"] == 5.00
    assert eva["actual_usd"] == 2.40
    # Came in 0.30 over the estimate, still under the 5.00 ceiling.
    assert eva["variance_usd"] == 0.30


def test_cost_routing_returns_actuals_without_a_routing_block(store):
    # No routing emitted (legacy / pre-RFC-0014 task) — actuals still surface.
    store.upsert_from_event(_code_terminal_event(key="99"))
    result = cost_routing(store, "99")
    assert result is not None
    assert result["routing"] is None
    assert result["actual_cost_usd"] == 2.40
    # Nothing to compare against, so no variance.
    assert result["estimate_vs_actual"]["estimate_usd"] is None
    assert result["estimate_vs_actual"]["variance_usd"] is None


def test_cost_routing_unknown_work_item_is_none(store):
    assert cost_routing(store, "does-not-exist") is None


def test_cost_routing_no_variance_for_nonmetered_run(store):
    # Subscription/local run: estimate is None on the routing block, so even with
    # actual tokens there is no metered $ and no variance to compute.
    routing = {**_ROUTING, "cost_estimate_usd": None}
    store.upsert_from_event(_plan_event(routing=routing, key="55"))
    local_event = CompletionEvent(
        correlation_key="55",
        service=Service.AIFACTORY,
        task_id="c-2",
        status="completed",
        phase="code",
        updated_at=datetime(2026, 6, 20, 12, 5, tzinfo=UTC),
        usage=TokenUsage(
            total_tokens=50_000,
            cost_usd=0.0,
            by_model={"ollama:qwen": {"total_tokens": 50_000, "cost_usd": 0.0, "workers": 1}},
            by_provider={
                "ollama": {"total_tokens": 50_000, "cost_usd": 0.0, "billing_mode": "local"}
            },
        ),
    )
    store.upsert_from_event(local_event)

    result = cost_routing(store, "55")
    assert result is not None
    assert result["estimate_vs_actual"]["estimate_usd"] is None
    assert result["estimate_vs_actual"]["variance_usd"] is None
    # Tokens still surface even with no metered cost.
    assert result["actual_total_tokens"] == 50_000


# --- API endpoint ---------------------------------------------------------


def test_cost_routing_endpoint(client, store):
    store.upsert_from_event(_plan_event(routing=_ROUTING))
    store.upsert_from_event(_code_terminal_event())

    resp = client.get("/api/tasks/42/cost-routing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["routing"]["rationale"].startswith("tier=medium")
    assert body["estimate_vs_actual"]["variance_usd"] == 0.30


def test_cost_routing_endpoint_404_for_unknown(client):
    resp = client.get("/api/tasks/nope/cost-routing")
    assert resp.status_code == 404

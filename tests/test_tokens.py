"""Tests for the token spine (RFC-0001 v1.1 usage block + aggregation)."""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory.copilot.tools import token_totals
from cfactory.models import CompletionEvent, Service, TokenUsage


def _ev(store, key, service, status, usage=None):
    store.upsert_from_event(
        CompletionEvent(
            correlation_key=key,
            service=service,
            task_id=f"{service.value}-t",
            status=status,
            phase=service.value,
            updated_at=datetime.now(UTC),
            usage=usage,
        )
    )


def _u(i, o, cost):
    return TokenUsage(input_tokens=i, output_tokens=o, total_tokens=i + o, cost_usd=cost)


def test_usage_persists_on_slice_and_timeline(store):
    _ev(store, "42", Service.AIFACTORY, "done", _u(100, 50, 0.25))
    wi = store.get("42")
    assert wi is not None and wi.aifactory.usage is not None
    assert wi.aifactory.usage.total_tokens == 150
    assert wi.aifactory.usage.cost_usd == 0.25
    assert wi.timeline[-1].usage is not None  # rides the event too


def test_no_usage_event_does_not_clobber_recorded_usage(store):
    # A live snapshot records usage; a later plain status event (no usage of its
    # own — e.g. human_review / failed) must NOT zero it out. Cost is
    # last-known-good (was: cockpit dropped real tokens back to 0).
    _ev(store, "99", Service.AIFACTORY, "running", _u(1000, 500, 1.5))
    _ev(store, "99", Service.AIFACTORY, "human_review", usage=None)
    wi = store.get("99")
    assert wi is not None and wi.aifactory.usage is not None
    assert wi.aifactory.usage.total_tokens == 1500
    assert wi.aifactory.usage.cost_usd == 1.5


def test_billing_summary_splits_metered_from_subscription(store):
    """A task that ran on a Claude subscription + a metered API provider: the row
    reports tokens for the subscription work and dollars only for the metered
    provider, plus the modes present and a wall-time elapsed (#96)."""
    usage = TokenUsage(
        input_tokens=1200,
        output_tokens=600,
        total_tokens=1800,
        cost_usd=0.4,
        by_provider={
            "claude": {
                "total_tokens": 1500,
                "cost_usd": 0.0,
                "workers": 2,
                "duration_ms": 12000,
                "billing_mode": "subscription",
            },
            "openai-compatible": {
                "total_tokens": 300,
                "cost_usd": 0.4,
                "workers": 1,
                "duration_ms": 4000,
                "billing_mode": "api",
            },
        },
    )
    _ev(store, "77", Service.AIFACTORY, "done", usage)
    out = token_totals(store)
    row = next(r for r in out["by_work_item"] if r["correlation_key"] == "77")
    b = row["billing"]
    assert sorted(b["modes"]) == ["api", "subscription"]
    assert b["metered_cost_usd"] == 0.4  # only the api provider counts as $
    assert b["nonmetered_tokens"] == 1500  # subscription tokens, shown without $
    assert b["has_metered"] is True
    assert b["by_mode"]["subscription"]["cost_usd"] == 0.0
    assert "elapsed_seconds" in row
    assert out["total"]["metered_cost_usd"] == 0.4  # fleet headline = real $ only
    assert out["total"]["has_billing_modes"] is True


def test_billing_summary_all_subscription_has_no_metered_cost(store):
    """Subscription-only task: no metered dollars; the fleet headline is 0 so the
    cockpit can hide the Cost (USD) stat (#96)."""
    usage = TokenUsage(
        input_tokens=800,
        output_tokens=400,
        total_tokens=1200,
        cost_usd=0.0,
        by_provider={
            "claude": {
                "total_tokens": 1200,
                "cost_usd": 0.0,
                "workers": 1,
                "billing_mode": "subscription",
            }
        },
    )
    _ev(store, "78", Service.AIFACTORY, "done", usage)
    out = token_totals(store)
    row = next(r for r in out["by_work_item"] if r["correlation_key"] == "78")
    assert row["billing"]["has_metered"] is False
    assert row["billing"]["metered_cost_usd"] == 0.0
    assert out["total"]["metered_cost_usd"] == 0.0


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


def test_token_totals_aggregates_all_three_services(store):
    # Now that PFactory (#60) and TFactory (#224) emit the usage block too, one
    # work item threaded across all three stages sums and flags each instrumented.
    _ev(store, "9", Service.PFACTORY, "emitted", _u(300, 60, 0.05))
    _ev(store, "9", Service.AIFACTORY, "done", _u(2000, 400, 0.50))
    _ev(store, "9", Service.TFACTORY, "triaged", _u(150, 30, 0.03))

    t = token_totals(store)
    assert t["total"]["total_tokens"] == 300 + 60 + 2000 + 400 + 150 + 30
    assert round(t["total"]["cost_usd"], 2) == 0.58
    for svc in ("pfactory", "aifactory", "tfactory"):
        assert t["by_service"][svc]["instrumented"] is True, svc
    # one work item carrying all three stages' usage
    assert t["by_work_item"][0]["correlation_key"] == "9"
    assert t["by_work_item"][0]["total_tokens"] == 2940


def test_usage_not_double_counted_on_duplicate(store):
    _ev(store, "1", Service.AIFACTORY, "done", _u(100, 50, 0.20))
    _ev(store, "1", Service.AIFACTORY, "done", _u(100, 50, 0.20))  # idempotent dup
    assert token_totals(store)["total"]["total_tokens"] == 150


def test_tokens_endpoint(client):
    client.post(
        "/api/events",
        json={
            "correlation_key": "7",
            "service": "aifactory",
            "task_id": "t",
            "status": "done",
            "updated_at": "2026-06-05T12:00:00Z",
            "usage": {
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
                "cost_usd": 0.10,
            },
        },
    )
    body = client.get("/api/tokens").json()
    assert body["total"]["total_tokens"] == 100
    assert body["by_service"]["aifactory"]["instrumented"] is True
    assert body["by_service"]["tfactory"]["instrumented"] is False

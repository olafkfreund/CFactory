"""Tests for the token spine (RFC-0001 v1.1 usage block + aggregation)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cfactory.copilot.tools import rollups, token_totals
from cfactory.models import CompletionEvent, Service, ServiceState, TokenUsage, WorkerUsage


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


def _verify(  # noqa: PLR0913 - concise event fixtures exercise distinct wire fields
    event_id, task_id, *, key="42", status="passed", minute=0, usage=None, **fields
):
    return CompletionEvent(
        id=event_id,
        correlation_key=key,
        service=Service.TFACTORY,
        task_id=task_id,
        status=status,
        time=datetime(2026, 11, 28, tzinfo=UTC) + timedelta(minutes=minute),
        usage=usage,
        **fields,
    )


def test_distinct_tfactory_receipts_reconcile_across_endpoints(client, store):
    pre = _verify(
        "pre-event",
        "pre",
        usage=TokenUsage(
            input_tokens=8,
            output_tokens=2,
            total_tokens=10,
            cost_usd=0.1,
            by_provider={"provider": {"total_tokens": 10, "cost_usd": 0.1, "billing_mode": "api"}},
            workers=[WorkerUsage(worker_id="overridden", total_tokens=10, billing_mode="local")],
        ),
    )
    post = _verify(
        "post-event",
        "post",
        status="failed",
        usage=TokenUsage(
            input_tokens=15,
            output_tokens=5,
            total_tokens=20,
            cost_usd=0.2,
            workers=[
                WorkerUsage(
                    worker_id="judge",
                    provider="mixed",
                    total_tokens=15,
                    duration_ms=500,
                    cost_usd=0.2,
                    billing_mode="cloud",
                ),
                WorkerUsage(
                    worker_id="check",
                    provider="mixed",
                    total_tokens=5,
                    duration_ms=300,
                    billing_mode="subscription",
                ),
            ],
        ),
    )
    for event in (pre, post, pre):
        assert client.post("/api/events", json=event.model_dump(mode="json")).status_code == 200

    wi = store.get("42")
    assert [event.id for event in wi.timeline] == ["pre-event", "post-event"]
    assert (wi.tfactory.task_id, wi.tfactory.status, wi.tfactory.usage.total_tokens) == (
        "post",
        "failed",
        20,
    )
    out = client.get("/api/tokens").json()
    for bucket in (out["total"], out["by_service"]["tfactory"], out["by_work_item"][0]):
        assert (bucket["input_tokens"], bucket["output_tokens"], bucket["total_tokens"]) == (
            23,
            7,
            30,
        )
        assert bucket["cost_usd"] == pytest.approx(0.3)
    billing = out["by_work_item"][0]["billing"]
    assert billing["modes"] == ["api", "cloud", "subscription"]
    assert billing["by_mode"]["api"]["cost_usd"] == 0.1
    assert billing["by_mode"]["cloud"]["cost_usd"] == 0.2
    assert billing["by_mode"]["subscription"] == {
        "total_tokens": 5,
        "cost_usd": 0.0,
        "duration_ms": 300,
    }
    assert out["total"]["metered_cost_usd"] == billing["metered_cost_usd"] == 0.3
    overview = client.get("/api/rollups").json()
    assert overview["cost"] == pytest.approx(0.3)
    assert overview["total_events"] == 2
    assert overview["by_status"] == {"failed": 1}


def test_tfactory_receipts_replace_by_canonical_time_then_timeline_order(store):
    for event in (
        _verify("first", "pre", minute=1, usage=_u(10, 0, 0.1)),
        _verify("cumulative", "pre", minute=3, usage=_u(15, 0, 0.15)),
        _verify("tie", "pre", minute=3, usage=_u(17, 0, 0.17)),
        _verify(
            "older",
            "pre",
            minute=2,
            usage=_u(12, 0, 0.12),
            updated_at=datetime(2026, 11, 29, tzinfo=UTC),
        ),
        _verify("unmeasured", "pre", minute=4),
        _verify("missing-identity", "", minute=4, usage=_u(100, 0, 1)),
        _verify("running", "another", minute=5, status="running", usage=_u(100, 0, 1)),
        _verify(
            "worker",
            "another",
            minute=6,
            phase="worker",
            status="worker_done",
            usage=_u(100, 0, 1),
            worker=WorkerUsage(
                worker_id="subtask", total_tokens=100, cost_usd=1, billing_mode="api"
            ),
        ),
        _verify(
            "heartbeat",
            "another",
            minute=7,
            phase="worker_progress",
            status="done",
            usage=_u(100, 0, 1),
            worker=WorkerUsage(worker_id="subtask", total_tokens=100),
        ),
        _verify("other-key", "pre", key="43", usage=_u(20, 0, 0.2)),
    ):
        store.upsert_from_event(event)

    assert store.get("42").tfactory.status == "running"
    out = token_totals(store)
    rows = {row["correlation_key"]: row for row in out["by_work_item"]}
    assert rows["42"]["total_tokens"] == 17
    assert rows["42"]["cost_usd"] == 0.17
    assert "billing" not in rows["42"]
    assert rows["43"]["total_tokens"] == 20
    assert out["total"]["total_tokens"] == 37
    assert rollups(store)["cost"] == pytest.approx(0.37)


def test_generated_empty_is_a_tfactory_terminal_receipt(store):
    store.upsert_from_event(_verify("verified", "pre", usage=_u(10, 0, 0.1)))
    store.upsert_from_event(
        _verify("empty", "post", status="generated_empty", usage=_u(20, 0, 0.2))
    )
    assert token_totals(store)["total"]["total_tokens"] == 30


def test_delayed_receipt_does_not_reverse_timeline_or_latency(client, store):
    store.upsert_from_event(_verify("new", "post", minute=1))
    store.upsert_from_event(_verify("delayed", "pre", minute=0))
    timeline = client.get("/api/workitems/42/timeline").json()
    assert [event["updated_at"] for event in timeline["events"]] == [
        "2026-11-28T00:00:00+00:00",
        "2026-11-28T00:01:00+00:00",
    ]
    assert timeline["span_seconds"] == 60
    assert client.get("/api/rollups").json()["latency"]["avg_seconds"] == 60


def test_tfactory_slice_fallback_yields_to_measured_zero(store):
    store.upsert_from_event(_verify("unmeasured", "pre"))
    store.upsert_snapshot(
        "42",
        Service.TFACTORY,
        ServiceState(
            task_id="pre",
            status="running",
            usage=_u(100, 0, 1),
            by_provider={"provider": {"total_tokens": 100, "cost_usd": 1, "billing_mode": "api"}},
        ),
    )
    out = token_totals(store)
    assert out["total"]["total_tokens"] == 100
    assert out["by_work_item"][0]["billing"]["metered_cost_usd"] == 1
    assert rollups(store)["cost"] == 1

    store.upsert_from_event(_verify("measured-zero", "pre", minute=1, usage=_u(0, 0, 0)))
    store.upsert_from_event(
        _verify("later-running", "pre", minute=2, status="running", usage=_u(100, 0, 1))
    )
    out = token_totals(store)
    assert out["by_service"]["tfactory"]["instrumented"] is True
    assert out["total"]["total_tokens"] == out["total"]["cost_usd"] == 0
    assert out["by_work_item"] == []
    assert rollups(store)["cost"] is None


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

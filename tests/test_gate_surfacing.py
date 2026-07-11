"""#167 (epic Factory#270): routing tier, security-gate verdicts and judge-vote
splits per task.

Covers the additive v1.3-style ingestion of the new optional envelope fields —
routing ``tier``/``tier_source``/``savings_usd`` (Factory#272), ``injection_scan``
(Factory#273), ``dependency_review`` (TFactory#650) and ``votes`` (TFactory#649)
— landing on the service slice's ``extra`` and round-tripping in the timeline;
the metered-only routing-savings rollup on ``/api/tokens``; and back-compat (old
envelopes without any of the new fields ingest and render unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory.copilot.tools import token_totals
from cfactory.models import CompletionEvent, Service, TokenUsage

_INJECTION = {"verdict": "flagged", "reason": "spec contains an override instruction"}
_DEP_REVIEW = {
    "status": "fail",
    "findings": [{"package": "leftpad", "severity": "high", "reason": "typosquat"}],
}
_VOTES = {
    "verdict": "pass",
    "majority": 2,
    "dissent": 1,
    "votes": [
        {"judge": "sonnet", "verdict": "pass"},
        {"judge": "opus", "verdict": "pass"},
        {"judge": "haiku", "verdict": "fail"},
    ],
}


def _event(service=Service.AIFACTORY, *, key="42", status="completed", **extra_fields):
    return CompletionEvent(
        correlation_key=key,
        service=service,
        task_id="t-1",
        status=status,
        updated_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        **extra_fields,
    )


def _metered_usage(*, savings=None):
    routing = {"routing_class": "standard", "tier": "economy", "tier_source": "policy"}
    if savings is not None:
        routing["savings_usd"] = savings
    return {
        "usage": TokenUsage(
            total_tokens=100_000,
            cost_usd=1.20,
            by_provider={
                "claude": {"total_tokens": 100_000, "cost_usd": 1.20, "billing_mode": "api"}
            },
        ),
        "routing": routing,
    }


# --- ingest: new fields land on the slice extra ----------------------------


def test_injection_scan_lands_on_slice_extra(store):
    item, _ = store.upsert_from_event(_event(injection_scan=_INJECTION))
    assert item.aifactory.extra["injection_scan"]["verdict"] == "flagged"
    assert "override instruction" in item.aifactory.extra["injection_scan"]["reason"]


def test_dependency_review_lands_on_slice_extra(store):
    item, _ = store.upsert_from_event(_event(Service.TFACTORY, dependency_review=_DEP_REVIEW))
    block = item.tfactory.extra["dependency_review"]
    assert block["status"] == "fail"
    assert block["findings"][0]["package"] == "leftpad"


def test_votes_land_on_slice_extra(store):
    item, _ = store.upsert_from_event(_event(Service.TFACTORY, votes=_VOTES))
    block = item.tfactory.extra["votes"]
    assert block["majority"] == 2
    assert block["dissent"] == 1
    assert len(block["votes"]) == 3


def test_routing_tier_lands_on_slice_extra(store):
    routing = {"routing_class": "standard", "tier": "economy", "tier_source": "policy"}
    item, _ = store.upsert_from_event(_event(routing=routing))
    block = item.aifactory.extra["routing"]
    assert block["tier"] == "economy"
    assert block["tier_source"] == "policy"


def test_new_fields_round_trip_in_timeline(store):
    item, _ = store.upsert_from_event(
        _event(injection_scan=_INJECTION, dependency_review=_DEP_REVIEW, votes=_VOTES)
    )
    ev = item.timeline[-1]
    assert ev.injection_scan == _INJECTION
    assert ev.dependency_review == _DEP_REVIEW
    assert ev.votes == _VOTES


# --- back-compat: old envelopes unchanged -----------------------------------


def test_old_envelope_without_new_fields_ingests_unchanged(store):
    item, applied = store.upsert_from_event(_event())
    assert applied
    assert item.aifactory.status == "completed"
    for field in ("injection_scan", "dependency_review", "votes", "routing"):
        assert field not in (item.aifactory.extra or {})


def test_old_envelope_ingests_via_api(client):
    resp = client.post(
        "/api/events",
        json={
            "correlation_key": "77",
            "service": "aifactory",
            "task_id": "t-77",
            "status": "completed",
            "updated_at": "2026-07-10T12:00:00Z",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_new_envelope_ingests_via_api(client, store):
    resp = client.post(
        "/api/events",
        json={
            "correlation_key": "78",
            "service": "tfactory",
            "task_id": "t-78",
            "status": "human_review",
            "updated_at": "2026-07-10T12:00:00Z",
            "injection_scan": _INJECTION,
            "dependency_review": _DEP_REVIEW,
            "votes": _VOTES,
        },
    )
    assert resp.status_code == 200
    item = store.get("78")
    assert item.tfactory.extra["injection_scan"]["verdict"] == "flagged"
    assert item.tfactory.extra["dependency_review"]["status"] == "fail"
    assert item.tfactory.extra["votes"]["majority"] == 2


# --- routing savings rollup (metered only) ----------------------------------


def test_routing_savings_summed_for_metered_tasks(store):
    store.upsert_from_event(_event(key="1", **_metered_usage(savings=0.80)))
    store.upsert_from_event(_event(key="2", **_metered_usage(savings=0.40)))

    totals = token_totals(store)
    assert totals["total"]["routing_savings_usd"] == 1.20
    rows = {r["correlation_key"]: r for r in totals["by_work_item"]}
    assert rows["1"]["routing_savings_usd"] == 0.80
    assert rows["2"]["routing_savings_usd"] == 0.40


def test_routing_savings_excluded_for_nonmetered_tasks(store):
    # A subscription run carries no real dollars — no notional savings either.
    event = _event(
        key="9",
        usage=TokenUsage(
            total_tokens=50_000,
            cost_usd=0.0,
            by_provider={
                "claude": {
                    "total_tokens": 50_000,
                    "cost_usd": 0.0,
                    "billing_mode": "subscription",
                }
            },
        ),
        routing={"tier": "economy", "tier_source": "policy", "savings_usd": 0.50},
    )
    store.upsert_from_event(event)

    totals = token_totals(store)
    assert "routing_savings_usd" not in totals["total"]
    assert all("routing_savings_usd" not in r for r in totals["by_work_item"])


def test_no_savings_key_when_no_routing_blocks(store):
    store.upsert_from_event(_event(key="5", usage=TokenUsage(total_tokens=10, cost_usd=0.01)))
    totals = token_totals(store)
    assert "routing_savings_usd" not in totals["total"]

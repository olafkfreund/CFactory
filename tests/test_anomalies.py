"""Tests for anomaly detection (#15)."""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.copilot.anomalies import detect_anomalies
from cfactory.models import CompletionEvent, Service


def _ev(store, key, service, status, when):
    store.upsert_from_event(
        CompletionEvent(
            correlation_key=key,
            service=service,
            task_id="t",
            status=status,
            phase=service.value,
            updated_at=when,
        )
    )


NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
FRESH = datetime(2026, 6, 5, 11, 50, tzinfo=timezone.utc)  # 10 min ago
OLD = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # days ago


def _kinds(anoms):
    return {a["kind"] for a in anoms}


def test_failure_flagged(store):
    _ev(store, "1", Service.TFACTORY, "triager_failed", FRESH)
    anoms = detect_anomalies(store, now=NOW)
    assert "failure" in _kinds(anoms)


def test_handback_loop_needs_two_failing_tests(store):
    _ev(store, "1", Service.TFACTORY, "rejected", FRESH)
    assert "handback_loop" not in _kinds(detect_anomalies(store, now=NOW))  # only one
    _ev(store, "1", Service.AIFACTORY, "coding", FRESH)
    _ev(store, "1", Service.TFACTORY, "rejected", FRESH)
    assert "handback_loop" in _kinds(detect_anomalies(store, now=NOW))  # two now


def test_stuck_when_stale_and_not_terminal(store):
    _ev(store, "1", Service.AIFACTORY, "coding", OLD)  # old, not terminal
    assert "stuck" in _kinds(detect_anomalies(store, now=NOW))


def test_not_stuck_when_terminal_or_fresh(store):
    _ev(store, "1", Service.TFACTORY, "triaged", OLD)  # old but terminal-ok
    _ev(store, "2", Service.AIFACTORY, "coding", FRESH)  # fresh
    assert "stuck" not in _kinds(detect_anomalies(store, now=NOW))


def test_anomalies_endpoint(client, store):
    _ev(store, "7", Service.AIFACTORY, "coding", OLD)  # stale -> stuck under real 'now'
    body = client.get("/api/anomalies").json()
    assert body["count"] >= 1
    assert "stuck" in {a["kind"] for a in body["anomalies"]}


# --------------------------------------------------------------------------
# #116: the O(n^2)->O(n) single-pass handback scan returns the same result.
# --------------------------------------------------------------------------


def _brute_force_handbacks(timeline) -> int:
    """Reference O(n^2) implementation the single-pass scan must agree with:
    a TFactory failure counts iff SOME later event is from AIFactory."""
    return sum(
        1
        for i, e in enumerate(timeline)
        if e.service is Service.TFACTORY
        and any(
            h in (e.status or "").lower() for h in ("fail", "reject", "block", "error", "stuck")
        )
        and any(later.service is Service.AIFACTORY for later in timeline[i + 1 :])
    )


def _handback_count(store, key) -> int:
    """Pull the handback_loop detail's leading integer for one work item, or 0."""
    for a in detect_anomalies(store, now=NOW):
        if a["correlation_key"] == key and a["kind"] == "handback_loop":
            return int(a["detail"].split()[0])
    return 0


def _build(store, key, steps):
    """Seed a timeline of (service, status) pairs with strictly increasing times
    so every event lands distinctly in the timeline (idempotency keys on id)."""
    base = datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc)
    for n, (service, status) in enumerate(steps):
        store.upsert_from_event(
            CompletionEvent(
                id=f"{key}-{n}",
                correlation_key=key,
                service=service,
                task_id="t",
                status=status,
                phase=service.value,
                updated_at=base.replace(minute=n),
            )
        )


def test_handback_scan_matches_brute_force(store):
    T, A, P = Service.TFACTORY, Service.AIFACTORY, Service.PFACTORY
    cases = {
        "none": [(P, "done"), (A, "coding")],  # no test failures
        "one_then_code": [(T, "rejected"), (A, "coding")],  # 1 bounce
        "fail_after_last_code": [  # trailing fail: no code after
            (T, "rejected"),
            (A, "coding"),
            (T, "triager_failed"),
        ],
        "two_bounces": [
            (T, "rejected"),
            (A, "coding"),
            (T, "triager_failed"),
            (A, "coding"),
        ],
        "passing_test": [(T, "passed"), (A, "coding")],  # not a failure status
    }
    for key, steps in cases.items():
        _build(store, key, steps)
    for key in cases:
        wi = store.get(key)
        expected = _brute_force_handbacks(wi.timeline)
        assert _handback_count(store, key) == expected, key

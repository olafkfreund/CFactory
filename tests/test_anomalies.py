"""Tests for anomaly detection (#15)."""

from __future__ import annotations

from datetime import UTC, datetime

import cfactory.store
import pytest
from cfactory.copilot.anomalies import detect_anomalies
from cfactory.models import CompletionEvent, Service, ServiceState
from cfactory.needs_you import needs_human


def _ev(store, key, service, status, when):
    # The store stamps updated_at with its own clock; pin it to ``when`` so the
    # liveness age is measured from the event's time, not the test's wall clock.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cfactory.store, "_now", lambda: when)
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


def _snap(store, key, service, status, when):
    """A polled snapshot: updates the stage slice with no timeline entry."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cfactory.store, "_now", lambda: when)
        store.upsert_snapshot(key, service, ServiceState(task_id="t", status=status))


NOW = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
FRESH = datetime(2026, 6, 5, 11, 50, tzinfo=UTC)  # 10 min ago
OLD = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # days ago


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


def test_not_stuck_when_polled_slice_is_done(store):
    # #454: the last EVENT says human_review, but the poll has since moved the
    # slice to done. The current status wins; the stale event must not flag it.
    _ev(store, "1", Service.AIFACTORY, "human_review", OLD)
    _snap(store, "1", Service.AIFACTORY, "done", OLD)
    assert "stuck" not in _kinds(detect_anomalies(store, now=NOW))


def test_review_is_not_stuck_but_needs_you(store):
    # #454: parked for a human is not hung. It belongs to needs-you, not anomalies.
    _ev(store, "1", Service.AIFACTORY, "human_review", OLD)
    assert "stuck" not in _kinds(detect_anomalies(store, now=NOW))
    wi = store.get("1")
    assert wi is not None and needs_human(wi)


def test_queued_backlog_is_not_stuck(store):
    # #458: a card nobody has started has no agent to hang. Not stuck.
    _snap(store, "1", Service.PFACTORY, "backlog", OLD)
    assert "stuck" not in _kinds(detect_anomalies(store, now=NOW))


def test_downstream_pending_is_not_stuck(store):
    _snap(store, "1", Service.AIFACTORY, "pending", OLD)
    assert "stuck" not in _kinds(detect_anomalies(store, now=NOW))


def test_discarded_is_not_a_failure(store):
    # #458: a discard is a deliberate close (e.g. the parr-regression teardown).
    _ev(store, "1", Service.PFACTORY, "discarded", FRESH)
    assert "failure" not in _kinds(detect_anomalies(store, now=NOW))


def test_rejected_is_still_a_failure(store):
    # Guards the discard exemption from widening to real failures.
    _ev(store, "1", Service.PFACTORY, "rejected", FRESH)
    assert "failure" in _kinds(detect_anomalies(store, now=NOW))


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
    base = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)
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
    t, a, p = Service.TFACTORY, Service.AIFACTORY, Service.PFACTORY
    cases = {
        "none": [(p, "done"), (a, "coding")],  # no test failures
        "one_then_code": [(t, "rejected"), (a, "coding")],  # 1 bounce
        "fail_after_last_code": [  # trailing fail: no code after
            (t, "rejected"),
            (a, "coding"),
            (t, "triager_failed"),
        ],
        "two_bounces": [
            (t, "rejected"),
            (a, "coding"),
            (t, "triager_failed"),
            (a, "coding"),
        ],
        "passing_test": [(t, "passed"), (a, "coding")],  # not a failure status
    }
    for key, steps in cases.items():
        _build(store, key, steps)
    for key in cases:
        wi = store.get(key)
        expected = _brute_force_handbacks(wi.timeline)
        assert _handback_count(store, key) == expected, key

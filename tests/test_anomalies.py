"""Tests for anomaly detection (#15)."""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.copilot.anomalies import detect_anomalies
from cfactory.models import CompletionEvent, Service


def _ev(store, key, service, status, when):
    store.upsert_from_event(CompletionEvent(
        correlation_key=key, service=service, task_id="t", status=status,
        phase=service.value, updated_at=when))


NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
FRESH = datetime(2026, 6, 5, 11, 50, tzinfo=timezone.utc)   # 10 min ago
OLD = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)      # days ago


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
    assert "handback_loop" in _kinds(detect_anomalies(store, now=NOW))      # two now


def test_stuck_when_stale_and_not_terminal(store):
    _ev(store, "1", Service.AIFACTORY, "coding", OLD)   # old, not terminal
    assert "stuck" in _kinds(detect_anomalies(store, now=NOW))


def test_not_stuck_when_terminal_or_fresh(store):
    _ev(store, "1", Service.TFACTORY, "triaged", OLD)   # old but terminal-ok
    _ev(store, "2", Service.AIFACTORY, "coding", FRESH)  # fresh
    assert "stuck" not in _kinds(detect_anomalies(store, now=NOW))


def test_anomalies_endpoint(client, store):
    _ev(store, "7", Service.AIFACTORY, "coding", OLD)   # stale -> stuck under real 'now'
    body = client.get("/api/anomalies").json()
    assert body["count"] >= 1
    assert "stuck" in {a["kind"] for a in body["anomalies"]}

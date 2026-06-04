"""Smoke tests for the skeleton API (#5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cfactory.app import create_app

client = TestClient(create_app())


def test_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "cfactory"
    assert "aifactory" in body["upstreams"]


def test_events_accepts_normalized_envelope():
    payload = {
        "correlation_key": "42",
        "service": "tfactory",
        "task_id": "spec-001",
        "status": "triaged",
        "phase": "test",
        "updated_at": "2026-06-04T12:00:00Z",
    }
    resp = client.post("/api/events", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted", "correlation_key": "42"}


def test_events_rejects_bad_service():
    payload = {
        "correlation_key": "42",
        "service": "not-a-service",
        "task_id": "x",
        "status": "done",
        "updated_at": "2026-06-04T12:00:00Z",
    }
    resp = client.post("/api/events", json=payload)
    assert resp.status_code == 422

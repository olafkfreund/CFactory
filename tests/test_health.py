"""Smoke tests for the skeleton API (#5)."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from cfactory import __version__


@pytest.mark.parametrize(
    "configured, expected", [(None, "dev"), ("", "dev"), ("sha-abc123", "sha-abc123")]
)
def test_version_from_environment(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, expected: str
) -> None:
    if configured is None:
        monkeypatch.delenv("CFACTORY_VERSION", raising=False)
    else:
        monkeypatch.setenv("CFACTORY_VERSION", configured)
    package = Path(__file__).resolve().parents[1] / "apps/backend/cfactory/__init__.py"
    assert runpy.run_path(str(package))["__version__"] == expected


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "cfactory"
    assert body["version"] == __version__
    assert "aifactory" in body["upstreams"]
    # The OpenObserve telemetry backend is listed for Services-view parity (#45).
    assert "observe" in body["upstreams"]


def test_events_accepts_normalized_envelope(client):
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


def test_events_rejects_bad_service(client):
    payload = {
        "correlation_key": "42",
        "service": "not-a-service",
        "task_id": "x",
        "status": "done",
        "updated_at": "2026-06-04T12:00:00Z",
    }
    resp = client.post("/api/events", json=payload)
    assert resp.status_code == 422

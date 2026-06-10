"""Tests for the cockpit broadcast hub (#10) via the TestClient WebSocket."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from cfactory.adapters import AIFactoryAdapter
from cfactory.app import adapters_dep, create_app, store_dep


def _event(key="99"):
    return {
        "correlation_key": key,
        "service": "aifactory",
        "task_id": "t1",
        "status": "coding",
        "phase": "code",
        "updated_at": "2026-06-04T12:00:00Z",
    }


def test_connect_sends_initial_snapshot(client):
    # A fresh client must receive the current state immediately on connect, so
    # a reconnecting cockpit is up to date without waiting for the next poll.
    with client.websocket_connect("/api/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert isinstance(msg["items"], list)


def test_event_broadcasts_to_connected_cockpit(client):
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"  # drain initial snapshot
        assert client.post("/api/events", json=_event("99")).status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "workitem"
        assert msg["item"]["correlation_key"] == "99"
        assert msg["item"]["aifactory"]["status"] == "coding"


def test_refresh_broadcasts_snapshot(store):
    ai = AIFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"tasks": [{"id": "t1", "status": "coding",
                                                           "metadata": {"githubIssueNumber": 5}}]})
        ),
    )
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[adapters_dep] = lambda: [ai]
    test_client = TestClient(app)

    with test_client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"  # drain initial snapshot
        test_client.post("/api/refresh")
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert any(item["correlation_key"] == "5" for item in msg["items"])

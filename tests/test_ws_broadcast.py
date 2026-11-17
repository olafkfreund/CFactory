"""Tests for the cockpit broadcast hub (#10) via the TestClient WebSocket."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from cfactory.adapters import AIFactoryAdapter
from cfactory.app import adapters_dep, create_app, store_dep
from cfactory.models import CompletionEvent, Service
from cfactory.ws import ConnectionManager
from fastapi.testclient import TestClient


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
            lambda r: httpx.Response(
                200, json={"tasks": [{"id": "t1", "status": "coding", "correlation_key": "5"}]}
            )
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


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_json(self, msg):
        self.sent.append(msg)


def _seed(store):
    for tenant, key in (("a", "ka"), ("b", "kb")):
        store.scoped(tenant).upsert_from_event(
            CompletionEvent(
                id=f"e-{key}",
                correlation_key=key,
                service=Service.PFACTORY,
                task_id="t",
                status="planned",
                phase="pfactory",
                updated_at=datetime(2026, 7, 17, tzinfo=UTC),
            )
        )


def test_snapshot_broadcast_is_per_tenant_when_multi_tenant(store):
    async def run(multi):
        m, wa, wb = ConnectionManager(), _FakeWS(), _FakeWS()
        await m.connect(wa, "a" if multi else None)
        await m.connect(wb, "b" if multi else None)
        await m.broadcast_snapshot(store, multi)
        return [[i["correlation_key"] for i in w.sent[0]["items"]] for w in (wa, wb)]

    _seed(store)
    assert asyncio.run(run(True)) == [["ka"], ["kb"]]
    a, b = asyncio.run(run(False))
    assert sorted(a) == sorted(b) == ["ka", "kb"]  # single-tenant: unchanged

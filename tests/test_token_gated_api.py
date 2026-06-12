"""Tests for the token-gated API (#73 follow-up).

When CFACTORY_API_KEYS is configured the keystore is ENFORCED: the HTTP
middleware requires a READ-scoped key on /api/* and /connect/*, the WebSocket
handlers reject unauthenticated sockets, write endpoints still require WRITE, and
a small set of paths stay exempt (/health probes, the inbound /api/events
webhook). In OPEN mode (no keys) everything is unchanged — covered by the rest of
the suite, which runs with the keystore open.

The middleware and WS handlers read the *global* keystore (they can't use the
``keystore_dep`` override), so these tests configure it via ``set_keys`` and
restore OPEN mode in teardown.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cfactory.app import create_app, store_dep
from cfactory.auth import reset_keystore, set_keys
from cfactory.store import WorkItemStore

RW = {"k_rw": {"read", "write"}}
RO = {"k_ro": {"read"}}


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def client(store):
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_keystore():
    # Every test here mutates the global keystore; always restore OPEN mode so the
    # rest of the suite (and other tests) are unaffected.
    yield
    reset_keystore()


# --------------------------------------------------------------------------
# HTTP middleware enforcement on /api/*
# --------------------------------------------------------------------------

def test_api_requires_key_when_configured(client):
    set_keys(RW)
    assert client.get("/api/workitems").status_code == 401


def test_api_accepts_valid_key(client):
    set_keys(RW)
    r = client.get("/api/workitems", headers={"Authorization": "Bearer k_rw"})
    assert r.status_code == 200


def test_api_accepts_x_api_key_header(client):
    set_keys(RW)
    r = client.get("/api/workitems", headers={"X-API-Key": "k_rw"})
    assert r.status_code == 200


def test_api_rejects_unknown_key(client):
    set_keys(RW)
    r = client.get("/api/workitems", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_open_mode_needs_no_key(client):
    # No set_keys() → OPEN: the data API is reachable with no auth (today's posture).
    assert client.get("/api/workitems").status_code == 200


# --------------------------------------------------------------------------
# Exemptions
# --------------------------------------------------------------------------

def test_health_is_exempt(client):
    set_keys(RW)
    assert client.get("/health").status_code == 200


def test_event_webhook_is_exempt(client):
    set_keys(RW)
    body = {
        "correlation_key": "555",
        "service": "aifactory",
        "task_id": "aifactory-task",
        "status": "done",
        "phase": "aifactory",
        "updated_at": "2026-06-04T15:00:00+00:00",
    }
    # No key supplied — the inbound webhook must still ingest (not 401).
    r = client.post("/api/events", json=body)
    assert r.status_code != 401
    assert r.json()["status"] in {"accepted", "duplicate"}


# --------------------------------------------------------------------------
# Write endpoints still require the WRITE scope
# --------------------------------------------------------------------------

def test_read_only_key_cannot_write(client):
    set_keys(RO)
    # READ passes the middleware but the write endpoint's require_scope("write")
    # rejects a read-only key.
    r = client.put(
        "/api/settings/copilot",
        json={"provider": "claude", "model": "claude-opus-4-8"},
        headers={"Authorization": "Bearer k_ro"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------
# /connect/vscode is guarded too (browser reaches it via nginx key-injection)
# --------------------------------------------------------------------------

def test_connect_requires_key_when_configured(client):
    set_keys(RW)
    r = client.get(
        "/connect/vscode",
        params={"redirect": "vscode://ext/cb", "state": "s"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_connect_with_key_redirects(client):
    set_keys(RW)
    r = client.get(
        "/connect/vscode",
        params={"redirect": "vscode://ext/cb", "state": "s"},
        headers={"Authorization": "Bearer k_rw"},
        follow_redirects=False,
    )
    assert r.status_code == 302


# --------------------------------------------------------------------------
# WebSocket auth
# --------------------------------------------------------------------------

def test_ws_rejects_without_key(client):
    set_keys(RW)
    with pytest.raises(Exception):  # noqa: B017 — Starlette closes the handshake
        with client.websocket_connect("/api/ws"):
            pass


def test_ws_accepts_with_key(client):
    set_keys(RW)
    with client.websocket_connect(
        "/api/ws", headers={"Authorization": "Bearer k_rw"}
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"


def test_ws_open_mode_no_key(client):
    with client.websocket_connect("/api/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"

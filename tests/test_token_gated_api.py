"""Tests for the token-gated API (#73 follow-up).

When CFACTORY_API_KEYS is configured the keystore is ENFORCED: the HTTP
middleware requires a READ-scoped key on /api/* and /connect/*, the WebSocket
handlers reject unauthenticated sockets, and write endpoints still require WRITE.
/health stays exempt. The inbound /api/events webhook needs WRITE, subject to
CFACTORY_EVENTS_AUTH_MODE (CFactory#501). With explicit dev-open and no keys,
the local API behavior is unchanged.

The middleware and WS handlers read the *global* keystore (they can't use the
``keystore_dep`` override), so these tests configure it via ``set_keys`` and
restore OPEN mode in teardown.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cfactory import app as app_module
from cfactory.app import create_app, store_dep
from cfactory.auth import get_keystore, reset_keystore, set_keys
from cfactory.config import get_settings, reset_settings
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


@pytest.mark.parametrize("dev_open", [False, True])
def test_api_rejects_unknown_key(client, monkeypatch, dev_open):
    set_keys(RW)
    monkeypatch.setattr(get_keystore(), "dev_open", dev_open)
    r = client.get("/api/workitems", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_open_mode_needs_no_key(client):
    # No set_keys() uses the suite's explicit dev-open setting.
    assert client.get("/api/workitems").status_code == 200


def test_keyless_api_denied_but_health_reachable(client, monkeypatch):
    set_keys({})
    monkeypatch.setattr(get_keystore(), "dev_open", False)
    r = client.get("/api/workitems")
    assert r.status_code == 401
    assert "CFACTORY_API_KEYS" in r.json()["detail"]
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/ws"):
        pass
    assert exc.value.code == 1008
    assert client.get("/health").status_code == 200


def test_get_keystore_reads_dev_open_env(monkeypatch):
    try:
        with monkeypatch.context() as env:
            env.setenv("CFACTORY_API_DEV_OPEN", "false")
            reset_settings()
            reset_keystore()
            assert get_keystore().dev_open is False
    finally:
        reset_settings()
        reset_keystore()


# --------------------------------------------------------------------------
# Exemptions
# --------------------------------------------------------------------------


def test_health_is_exempt(client):
    set_keys(RW)
    assert client.get("/health").status_code == 200


_EVENT = {
    "correlation_key": "555",
    "service": "aifactory",
    "task_id": "aifactory-task",
    "status": "done",
    "phase": "aifactory",
    "updated_at": "2026-06-04T15:00:00+00:00",
}


def _events_mode(monkeypatch, mode: str | None) -> None:
    if mode is None:
        monkeypatch.delenv("CFACTORY_EVENTS_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("CFACTORY_EVENTS_AUTH_MODE", mode)
    reset_settings()


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    reset_settings()


@pytest.mark.parametrize("keyless", [False, True])
def test_event_webhook_warn_mode_accepts_and_logs(client, monkeypatch, caplog, keyless):
    # Default mode is warn (CFactory#501): an unauthenticated event still ingests,
    # but is logged and counted.
    _events_mode(monkeypatch, None)
    set_keys({} if keyless else RW)
    if keyless:
        monkeypatch.setattr(get_keystore(), "dev_open", False)
    before = app_module._unauthenticated_events.total()
    with caplog.at_level("WARNING", logger="cfactory.app"):
        r = client.post("/api/events", json=_EVENT)
    assert r.status_code == 200
    assert r.json()["status"] in {"accepted", "duplicate"}
    assert app_module._unauthenticated_events.total() == before + 1
    assert "warn mode" in caplog.text


def test_event_webhook_warn_mode_does_not_count_a_write_key(client, monkeypatch, caplog):
    _events_mode(monkeypatch, "warn")
    set_keys(RW)
    before = app_module._unauthenticated_events.total()
    with caplog.at_level("WARNING", logger="cfactory.app"):
        r = client.post("/api/events", json=_EVENT, headers={"Authorization": "Bearer k_rw"})
    assert r.status_code == 200
    assert app_module._unauthenticated_events.total() == before
    assert "warn mode" not in caplog.text


def test_event_webhook_warn_log_never_contains_the_key(client, monkeypatch, caplog):
    _events_mode(monkeypatch, "warn")
    set_keys(RW)
    with caplog.at_level("WARNING", logger="cfactory.app"):
        client.post("/api/events", json=_EVENT, headers={"Authorization": "Bearer k_ro_secret"})
    assert "warn mode" in caplog.text
    assert "k_ro_secret" not in caplog.text


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer nope"}, 401),
        ({"Authorization": "Bearer k_ro"}, 403),
        ({"Authorization": "Bearer k_rw"}, 200),
        ({"X-API-Key": "k_rw"}, 200),
    ],
)
def test_event_webhook_enforce_mode_requires_write(client, monkeypatch, headers, status):
    _events_mode(monkeypatch, "ENFORCE")
    set_keys({**RW, **RO})
    assert client.post("/api/events", json=_EVENT, headers=headers).status_code == status
    assert client.post("/api/events/completion", json=_EVENT, headers=headers).status_code == status


def test_event_webhook_unknown_mode_fails_closed(client, monkeypatch):
    _events_mode(monkeypatch, "warning")
    set_keys(RW)
    assert client.post("/api/events", json=_EVENT).status_code == 401


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("1", False),
        ("yes", False),
        ("on", False),
        ("t", False),
        ("false", False),
        ("", False),
    ],
)
@pytest.mark.parametrize("var", ["CFACTORY_API_DEV_OPEN", "CFACTORY_MCP_DEV_OPEN"])
def test_dev_open_flags_parse_strictly(monkeypatch, var, raw, expected):
    monkeypatch.setenv(var, raw)
    reset_settings()
    field = var.removeprefix("CFACTORY_").lower()
    assert getattr(get_settings(), field) is expected


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
    with client.websocket_connect("/api/ws", headers={"Authorization": "Bearer k_rw"}) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"


def test_ws_open_mode_no_key(client):
    with client.websocket_connect("/api/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"

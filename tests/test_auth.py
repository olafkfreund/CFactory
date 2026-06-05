"""Tests for scoped API-key auth (#20).

Local-first posture: with NO keys configured the API is OPEN and the write
endpoint works with no auth header. With keys configured, ``POST
/api/actions/execute`` requires a key bearing the ``write`` scope.

Hermetic: the executor uses an injected ``httpx.MockTransport`` and the keystore
is injected via the ``keystore_dep`` dependency override — no real env juggling.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from cfactory.app import action_transport_dep, create_app, store_dep
from cfactory.auth import KeyStore, keystore_dep, parse_api_keys

# A valid PreparedAction body for the execute endpoint.
EXECUTE_BODY = {
    "kind": "trigger_handoff",
    "correlation_key": "42",
    "target_service": "aifactory",
    "method": "POST",
    "endpoint": "/api/tasks/create-and-run",
    "payload": {"correlation_key": "42", "issue_number": 42},
    "rationale": "hand off",
}


class _OkTransport(httpx.MockTransport):
    """MockTransport that answers every request with 200 {"ok": true}."""

    def __init__(self):
        super().__init__(lambda request: httpx.Response(200, json={"ok": True}))


def _make_client(store, keys: dict[str, set[str]] | None) -> TestClient:
    """Build a TestClient with a hermetic store, mock transport, and (optionally)
    a configured keystore. ``keys=None`` leaves the keystore OPEN (local mode)."""
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[action_transport_dep] = lambda: _OkTransport()
    if keys is not None:
        app.dependency_overrides[keystore_dep] = lambda: KeyStore(keys)
    return TestClient(app)


# --------------------------------------------------------------------------
# parse_api_keys unit tests
# --------------------------------------------------------------------------

def test_parse_api_keys_empty():
    assert parse_api_keys(None) == {}
    assert parse_api_keys("") == {}


def test_parse_api_keys_multiple_entries_and_scopes():
    parsed = parse_api_keys("acw_read:read; acw_rw:read,write")
    assert parsed == {"acw_read": {"read"}, "acw_rw": {"read", "write"}}


def test_parse_api_keys_tolerates_whitespace_and_empty_entries():
    parsed = parse_api_keys("  k1 : read , write ;;  ; k2:read ")
    assert parsed == {"k1": {"read", "write"}, "k2": {"read"}}


def test_parse_api_keys_key_without_scopes_has_empty_set():
    assert parse_api_keys("naked") == {"naked": set()}


# --------------------------------------------------------------------------
# Local mode: no keys configured -> execute is OPEN
# --------------------------------------------------------------------------

def test_execute_open_in_local_mode_no_header(store):
    """No keys configured: execute works with no Authorization header."""
    api = _make_client(store, keys=None)
    resp = api.post("/api/actions/execute", json=EXECUTE_BODY)
    assert resp.status_code == 200
    assert resp.json() == {"status_code": 200, "ok": True, "body": {"ok": True}}


def test_execute_open_when_keystore_explicitly_empty(store):
    """An explicitly empty keystore is still OPEN mode."""
    api = _make_client(store, keys={})
    resp = api.post("/api/actions/execute", json=EXECUTE_BODY)
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Keys configured: enforce read/write scopes on execute
# --------------------------------------------------------------------------

@pytest.fixture
def keyed_client(store):
    keys = {"read-key": {"read"}, "rw-key": {"read", "write"}}
    return _make_client(store, keys=keys)


def test_execute_no_key_is_401(keyed_client):
    resp = keyed_client.post("/api/actions/execute", json=EXECUTE_BODY)
    assert resp.status_code == 401


def test_execute_invalid_key_is_401(keyed_client):
    resp = keyed_client.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"Authorization": "Bearer not-a-real-key"},
    )
    assert resp.status_code == 401


def test_execute_read_only_key_is_403(keyed_client):
    resp = keyed_client.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"Authorization": "Bearer read-key"},
    )
    assert resp.status_code == 403


def test_execute_write_key_is_allowed(keyed_client):
    resp = keyed_client.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"Authorization": "Bearer rw-key"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status_code": 200, "ok": True, "body": {"ok": True}}


def test_execute_write_key_via_x_api_key_header(keyed_client):
    """The X-API-Key header is accepted as an alternative to Bearer."""
    resp = keyed_client.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"X-API-Key": "rw-key"},
    )
    assert resp.status_code == 200

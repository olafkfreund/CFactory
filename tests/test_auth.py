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
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cfactory.app import action_transport_dep, audit_dep, create_app, store_dep
from cfactory.audit import AuditStore
from cfactory.auth import READ, WRITE, KeyStore, keystore_dep, parse_api_keys, secret_matches

# A valid PreparedAction body for the execute endpoint.
EXECUTE_BODY = {
    "kind": "recover",
    "correlation_key": "42",
    "target_service": "aifactory",
    "method": "POST",
    "endpoint": "/api/tasks/ai-7/recover",
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
    # Hermetic in-memory audit store so execute never touches the workspace DB.
    app.dependency_overrides[audit_dep] = lambda: AuditStore("sqlite://")
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
    _r = resp.json()
    assert _r["status_code"] == 200 and _r["ok"] is True and _r["body"] == {"ok": True}


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
    _r = resp.json()
    assert _r["status_code"] == 200 and _r["ok"] is True and _r["body"] == {"ok": True}


def test_execute_write_key_via_x_api_key_header(keyed_client):
    """The X-API-Key header is accepted as an alternative to Bearer."""
    resp = keyed_client.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"X-API-Key": "rw-key"},
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# #113: constant-time compare. The keystore now matches keys with
# hmac.compare_digest in a non-short-circuiting loop. Prove the constant-time
# path still authenticates correctly (right scope for the right key, unknown
# keys rejected) — i.e. behaviour is identical to the old dict lookup.
# --------------------------------------------------------------------------


def test_secret_matches_constant_time_helper():
    assert secret_matches("abc", "abc") is True
    assert secret_matches("abc", "abd") is False
    assert secret_matches("abc", "abcd") is False  # length mismatch
    assert secret_matches(None, "abc") is False
    assert secret_matches("abc", None) is False
    assert secret_matches("", "") is False  # empty secret never matches


def test_keystore_scopes_for_constant_time_still_resolves():
    ks = KeyStore({"read-key": {READ}, "rw-key": {READ, WRITE}})
    # Exact match resolves to the right scope set, regardless of dict position.
    assert ks.scopes_for("read-key") == {READ}
    assert ks.scopes_for("rw-key") == {READ, WRITE}
    # Unknown / near-miss / None keys resolve to None (unknown), not a crash.
    assert ks.scopes_for("nope") is None
    assert ks.scopes_for("read-ke") is None  # prefix is not a match
    assert ks.scopes_for(None) is None


def test_keystore_authorize_constant_time_path():
    """End-to-end: a valid write key authorizes; wrong scope -> 403; unknown -> 401."""
    ks = KeyStore({"read-key": {READ}, "rw-key": {READ, WRITE}})
    ks.authorize("rw-key", WRITE)  # valid: no raise
    ks.authorize("read-key", READ)  # valid: no raise
    with pytest.raises(HTTPException) as ei403:
        ks.authorize("read-key", WRITE)  # has key, lacks scope
    assert ei403.value.status_code == 403
    with pytest.raises(HTTPException) as ei401:
        ks.authorize("bogus", READ)  # unknown key
    assert ei401.value.status_code == 401

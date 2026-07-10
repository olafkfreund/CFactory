"""Tests for the enterprise identity seam (#21): the audit-actor identity.

Covers the identity seam that stamps the audit actor. Full SAML/SCIM IdP
integration and per-tenant query scoping are DEFERRED to the hosted deployment
and are intentionally not exercised here.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfactory.app import action_transport_dep, audit_dep, create_app, store_dep
from cfactory.auth import KeyStore, keystore_dep
from cfactory.enterprise import LOCAL_IDENTITY, identity_dep

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
    def __init__(self):
        super().__init__(lambda request: httpx.Response(200, json={"ok": True}))


def _client(store, audit, keys):
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = lambda: _OkTransport()
    app.dependency_overrides[keystore_dep] = lambda: KeyStore(keys)
    return TestClient(app)


# --------------------------------------------------------------------------
# identity seam wiring into the audit actor
# --------------------------------------------------------------------------

@pytest.fixture
def audit(tmp_path):
    from cfactory.audit import AuditStore

    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}")


def test_actor_is_local_in_open_mode(store, audit):
    api = _client(store, audit, keys={})  # OPEN
    resp = api.post("/api/actions/execute", json=EXECUTE_BODY)
    assert resp.status_code == 200
    assert audit.list()[0].actor == LOCAL_IDENTITY


def test_actor_is_api_key_when_keys_configured(store, audit):
    api = _client(store, audit, keys={"rw-key": {"read", "write"}})
    resp = api.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"Authorization": "Bearer rw-key"},
    )
    assert resp.status_code == 200
    assert audit.list()[0].actor == "rw-key"


def test_identity_dep_is_overridable():
    """identity_dep is a seam: a hosted auth integration can replace it wholesale."""
    app = FastAPI()

    @app.get("/whoami")
    def whoami(actor: str = __import__("fastapi").Depends(identity_dep)):
        return {"actor": actor}

    app.dependency_overrides[identity_dep] = lambda: "saml|alice@corp"
    client = TestClient(app)
    assert client.get("/whoami").json() == {"actor": "saml|alice@corp"}

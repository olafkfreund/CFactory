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
from cfactory.auth import KeyStore, key_actor, keystore_dep, reset_keystore, set_keys
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


def test_actor_is_never_the_api_key(store, audit):
    """#251: the presented key must NOT become the audit actor.

    It used to. The trail is rendered in the cockpit's Audit view and returned
    by ``GET /api/audit``, so storing the key there handed a working
    write-scoped credential to anyone with read access to the trail.
    """
    api = _client(store, audit, keys={"rw-key": {"read", "write"}})
    resp = api.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"Authorization": "Bearer rw-key"},
    )
    assert resp.status_code == 200
    actor = audit.list()[0].actor
    assert "rw-key" not in actor
    # Honest about what it does and does not know: a key is a client, not a
    # person, so the actor says unattributed and carries a stable reference to
    # WHICH key acted rather than inventing a plausible-looking name.
    assert actor == key_actor("rw-key")
    assert actor.startswith("unattributed:key-")


def test_key_actor_is_stable_and_distinguishes_keys():
    assert key_actor("rw-key") == key_actor("rw-key")
    assert key_actor("rw-key") != key_actor("ro-key")


def test_audit_store_redacts_a_live_key_passed_straight_in(audit):
    """Backstop: not every audit write goes through ``identity_dep``.

    ``AuditStore.record`` is the single write path, so the guard lives there
    too — a future route that hand-builds an actor cannot reintroduce #251.
    """
    set_keys({"rw-key": {"read", "write"}})
    try:
        entry = audit.record(
            actor="rw-key",
            kind="approve_review",
            correlation_key="42",
            target_service="aifactory",
            endpoint="/api/tasks/ai-7/approve",
            status_code=200,
            ok=True,
        )
        assert entry.actor == key_actor("rw-key")
    finally:
        reset_keystore()


def test_rows_written_before_the_fix_do_not_serve_a_live_key(audit):
    """Historical rows still hold the raw key; reads must not hand it back.

    The entries are HMAC-chained, so the stored rows are NOT rewritten (that is
    indistinguishable from tampering) — the read path redacts instead. Retiring
    the key is the remediation for the copy at rest.
    """
    set_keys({})  # no keys configured yet: the raw actor is stored verbatim
    audit.record(
        actor="rw-key",
        kind="approve_review",
        correlation_key="42",
        target_service="aifactory",
        endpoint="/api/tasks/ai-7/approve",
        status_code=200,
        ok=True,
    )
    set_keys({"rw-key": {"read", "write"}})
    try:
        assert audit.list()[0].actor == key_actor("rw-key")
        # The chain still verifies: no stored row was touched.
        assert audit.verify() == []
    finally:
        reset_keystore()


def test_identity_dep_is_overridable():
    """identity_dep is a seam: a hosted auth integration can replace it wholesale."""
    app = FastAPI()

    @app.get("/whoami")
    def whoami(actor: str = __import__("fastapi").Depends(identity_dep)):
        return {"actor": actor}

    app.dependency_overrides[identity_dep] = lambda: "saml|alice@corp"
    client = TestClient(app)
    assert client.get("/whoami").json() == {"actor": "saml|alice@corp"}

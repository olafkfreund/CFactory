"""Tests for the enterprise identity seam (#21): the audit-actor identity.

Covers the identity seam that stamps the audit actor. Full SAML/SCIM IdP
integration and per-tenant query scoping are DEFERRED to the hosted deployment
and are intentionally not exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cfactory import enterprise
from cfactory.app import action_transport_dep, audit_dep, create_app, store_dep
from cfactory.audit import redact_actor
from cfactory.auth import KeyStore, key_actor, keystore_dep, reset_keystore, set_keys
from cfactory.config import Settings
from cfactory.enterprise import LOCAL_IDENTITY, identity_dep, oidc_actor
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    app.dependency_overrides[action_transport_dep] = _OkTransport
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


# --------------------------------------------------------------------------
# #251 part b — the trail names the PERSON, and only on proof
# --------------------------------------------------------------------------
#
# Part a stopped the trail carrying a live credential; it left the actor naming
# a shared client. These cover the other half: a confirmed action is attributed
# to the human who confirmed it — but ONLY when an ID token verifies, because an
# audit trail that can be made to name the wrong person is worse than one that
# admits it does not know.

ISSUER = "https://keycloak.test/realms/factory"

# Generated once: 2048-bit keygen is not free, and every test wants the same
# "the IdP's key" vs "some other key" pair.
_IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _id_token(key: object = _IDP_KEY, algorithm: str = "RS256", **claims: object) -> str:
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": "cfactory",
        "sub": "24a1f0c8-0000-4000-8000-abcdef123456",
        "email": "alice@example.com",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm=algorithm)


def _stub_jwks(monkeypatch) -> None:
    """Stub ONLY the JWKS *fetch*. ``jwt.decode`` still runs the real signature,
    ``exp`` and ``iss`` checks against the real public key, so these tests
    exercise the verification rather than a mock of it."""
    monkeypatch.setattr(
        enterprise,
        "_jwks_client",
        lambda _s: SimpleNamespace(
            get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=_IDP_KEY.public_key())
        ),
    )


@pytest.fixture
def oidc(monkeypatch):
    """Issuer configured, JWKS fetch stubbed. Returns the Settings under test."""
    settings = Settings(oidc_issuer=ISSUER)
    monkeypatch.setattr(enterprise, "get_settings", lambda: settings)
    _stub_jwks(monkeypatch)
    return settings


@pytest.mark.usefixtures("oidc")
def test_actor_is_the_person_who_confirmed_the_action(store, audit):
    """The point of the whole issue: "who approved this" has an answer.

    An ``unattributed:key-…`` actor names a client every cockpit user shares. EU
    AI Act Article 14 oversight — which the HITL demo claims — is about a NAMED
    human, so a verified ID token beats the key reference as the actor.
    """
    api = _client(store, audit, keys={"rw-key": {"read", "write"}})
    resp = api.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={
            "Authorization": "Bearer rw-key",
            "X-Forwarded-Id-Token": f"Bearer {_id_token()}",
        },
    )
    assert resp.status_code == 200
    assert audit.list()[0].actor == "user:alice@example.com"


@pytest.mark.usefixtures("oidc")
def test_the_key_is_still_absent_when_a_person_is_named(store, audit):
    """Part a must not regress through the new path."""
    api = _client(store, audit, keys={"rw-key": {"read", "write"}})
    api.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={
            "Authorization": "Bearer rw-key",
            "X-Forwarded-Id-Token": f"Bearer {_id_token()}",
        },
    )
    assert "rw-key" not in audit.list()[0].actor


@pytest.mark.usefixtures("oidc")
def test_a_token_signed_by_anyone_else_names_nobody(store, audit):
    """THE trust-boundary test.

    The backend is also reachable on the direct-to-backend editor host, where a
    write-scoped key is the only gate — so the identity in the request is
    attacker-typed. A plaintext ``X-Auth-Request-Email`` header would be forged
    there outright; a signature cannot be, and this proves the difference.
    """
    api = _client(store, audit, keys={"rw-key": {"read", "write"}})
    forged = _id_token(key=_OTHER_KEY, email="ceo@example.com")
    resp = api.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"Authorization": "Bearer rw-key", "X-Forwarded-Id-Token": f"Bearer {forged}"},
    )
    assert resp.status_code == 200
    # Falls back to the honest key reference — never to the forged name.
    assert audit.list()[0].actor == key_actor("rw-key")


@pytest.mark.usefixtures("oidc")
def test_garbage_in_the_header_does_not_break_the_action(store, audit):
    """Fail secure, not fail closed: identity is a label on an already-authorized
    request, so an unparseable token must not 500 a confirmed HITL action."""
    api = _client(store, audit, keys={"rw-key": {"read", "write"}})
    resp = api.post(
        "/api/actions/execute",
        json=EXECUTE_BODY,
        headers={"Authorization": "Bearer rw-key", "X-Forwarded-Id-Token": "Bearer not.a.jwt"},
    )
    assert resp.status_code == 200
    assert audit.list()[0].actor == key_actor("rw-key")


def test_expired_token_names_nobody(oidc):
    stale = _id_token(exp=datetime.now(UTC) - timedelta(seconds=1))
    assert oidc_actor(f"Bearer {stale}", oidc) is None


def test_token_from_another_issuer_names_nobody(oidc):
    assert oidc_actor(f"Bearer {_id_token(iss='https://evil.test/realms/x')}", oidc) is None


def test_token_with_no_expiry_names_nobody(oidc):
    """``require: ["exp"]`` — an everlasting token would make the replay window
    infinite, and PyJWT does not demand ``exp`` unless asked to."""
    payload = {"iss": ISSUER, "email": "alice@example.com"}
    assert oidc_actor(f"Bearer {jwt.encode(payload, _IDP_KEY, algorithm='RS256')}", oidc) is None


@pytest.mark.parametrize(
    ("algorithm", "key"),
    [("HS256", "a-shared-secret-the-attacker-picked"), ("none", None)],
)
def test_symmetric_and_unsigned_algorithms_are_refused(oidc, algorithm, key):
    """Algorithm confusion. With HS256 in the accepted list, a token HMAC'd with
    the issuer's PUBLIC key verifies — the public key is public. ``none`` needs
    no key at all. Both must be refused by the allowlist, not by luck."""
    assert oidc_actor(f"Bearer {_id_token(key=key, algorithm=algorithm)}", oidc) is None


def test_no_id_token_leaves_the_key_actor_untouched(oidc):
    assert oidc_actor(None, oidc) is None
    assert oidc_actor("", oidc) is None
    assert oidc_actor("Bearer ", oidc) is None


def test_unconfigured_issuer_never_reads_the_token(monkeypatch):
    """No IdP configured (the local/dev default) means no attested person — and
    the token is not even parsed, so nothing about it can influence the trail."""
    monkeypatch.setattr(
        enterprise,
        "_jwks_client",
        lambda _s: pytest.fail("JWKS must not be consulted with no issuer configured"),
    )
    assert oidc_actor(f"Bearer {_id_token()}", Settings(oidc_issuer=None)) is None


def test_audience_is_enforced_once_configured(monkeypatch):
    _stub_jwks(monkeypatch)
    settings = Settings(oidc_issuer=ISSUER, oidc_audience="cfactory")
    assert oidc_actor(f"Bearer {_id_token()}", settings) == "user:alice@example.com"
    assert oidc_actor(f"Bearer {_id_token(aud='some-other-client')}", settings) is None


def test_falls_back_through_the_claims_to_something_durable(oidc):
    """No email released by the IdP is not a reason to give up on naming anyone."""
    no_email = _id_token(email=None, preferred_username="alice")
    assert oidc_actor(f"Bearer {no_email}", oidc) == "user:alice"
    bare = _id_token(email=None)
    assert oidc_actor(f"Bearer {bare}", oidc) == "user:24a1f0c8-0000-4000-8000-abcdef123456"


def test_a_hostile_claim_cannot_forge_or_overflow_the_actor_column(oidc):
    """``audit_entries.actor`` is String(128): Postgres REFUSES an over-long
    value, which would turn a confirmed action into a 500 at the audit write —
    after the upstream call already happened. And a claim carrying a newline
    could fake a second row in an exported trail."""
    long_actor = oidc_actor(f"Bearer {_id_token(email='a' * 400 + '@example.com')}", oidc)
    assert long_actor is not None
    assert len(long_actor) <= 120

    injected = _id_token(email="alice@x\nuser:ceo@example.com", sub="s-1")
    assert oidc_actor(f"Bearer {injected}", oidc) == "user:s-1"


def test_the_cockpit_proxy_still_forwards_the_id_token():
    """The one link in this chain that cannot fail loudly.

    The backend verifies whatever it is handed, so if nginx stops forwarding the
    ID token nothing errors — every actor just quietly reverts to
    ``unattributed`` and the trail stops naming anyone, which is the exact
    regression #251 is about. The header is only needed BECAUSE the line below
    it overwrites ``Authorization`` (which is where oauth2-proxy put the token),
    so assert the two together: they are one edit apart from silently undoing
    each other.
    """
    template = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend-web" / "nginx.conf.template"
    ).read_text(encoding="utf-8")
    api_block = template.split("location /api/")[1].split("location ")[0]
    assert "proxy_set_header X-Forwarded-Id-Token $http_authorization;" in api_block
    assert "proxy_set_header Authorization" in api_block


def test_both_jwks_fetches_send_a_browser_user_agent(monkeypatch):
    """The other link that cannot fail loudly, found on the live cluster.

    PyJWKClient fetches the key set with ``urllib``, whose default
    ``Python-urllib/3.x`` User-Agent Cloudflare — which fronts this deployment's
    Keycloak — answers with a 403. Every failure in this seam degrades quietly
    to ``unattributed``, so that 403 is indistinguishable from "no IdP
    configured": the trail would name nobody and nothing would go red. Both
    fetches must carry the UA, so pin both.
    """
    sent: dict[str, object] = {}

    def _fake_get(_url, *, headers=None, **_kw):
        sent["discovery_ua"] = (headers or {}).get("User-Agent")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"jwks_uri": "https://idp.test/certs"},
        )

    def _fake_client(_uri, *, headers=None, **_kw):
        sent["jwks_ua"] = (headers or {}).get("User-Agent")
        return SimpleNamespace()

    monkeypatch.setattr(enterprise.httpx, "get", _fake_get)
    monkeypatch.setattr(enterprise.jwt, "PyJWKClient", _fake_client)
    enterprise._discover_jwks_client.cache_clear()

    enterprise._jwks_client(Settings(oidc_issuer=ISSUER))

    # Cloudflare's bot rules key off the Mozilla prefix, not the whole string.
    assert str(sent["discovery_ua"]).startswith("Mozilla/5.0")
    assert str(sent["jwks_ua"]).startswith("Mozilla/5.0")


def test_rotating_the_key_cleans_the_old_rows_up_instead_of_un_redacting_them(audit):
    """The remediation for the 126 rows already at rest must not backfire.

    Those rows hold the key in clear and are not rewritten — the HMAC chain
    makes that indistinguishable from tampering — so retiring the key is the
    fix. But redacting only a CURRENTLY configured key means the moment it
    leaves ``CFACTORY_API_KEYS`` the trail serves the stored string verbatim
    again: dead, yet a 40-character opaque token rendered in the Audit view,
    indistinguishable on screen from the live one that started #251. Shape is
    checked too, so rotation cleans up rather than un-redacts.
    """
    stale = "acw_f47824bd" + "0" * 28  # a retired key's SHAPE, not a live value
    set_keys({})  # written before the keystore existed, and never rotated back
    audit.record(
        actor=stale,
        kind="approve_review",
        correlation_key="42",
        target_service="aifactory",
        endpoint="/api/tasks/ai-7/approve",
        status_code=200,
        ok=True,
    )
    try:
        served = audit.list()[0].actor
        assert stale not in served
        assert served == key_actor(stale)
        assert audit.verify() == []  # no stored row was touched
    finally:
        reset_keystore()


@pytest.mark.parametrize(
    "actor",
    ["system", "local", "user:alice@example.com", "unattributed:key-9986a9f11017", "acw_short"],
)
def test_shape_redaction_leaves_real_actors_alone(actor):
    """The guard must not eat the identities the seam legitimately produces —
    a redaction that turns every row into a digest trades one uselessness for
    another."""
    set_keys({})
    try:
        assert redact_actor(actor) == actor
    finally:
        reset_keystore()

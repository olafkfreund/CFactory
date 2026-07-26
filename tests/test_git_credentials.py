"""Encrypted per-tenant git credentials (RFC-0020 §3.4, #364).

This is the phase where the real risk sits, so the properties are pinned one
test each and three of them are mutation-checked — remove the guard and a test
here must go red:

* **encrypted at rest** — no column of the stored row contains the credential,
  in any encoding. Store the plaintext instead and
  ``test_nothing_stored_anywhere_contains_the_credential`` fails;
* **never returned, never logged** — no API response, error, audit entry or log
  record carries it. Let it into a response or a log line and
  ``test_no_surface_ever_returns_the_credential`` /
  ``test_nothing_is_ever_logged_that_contains_the_credential`` fail;
* **tenant isolation** — tenant A cannot read, use or overwrite tenant B's, and
  the tenant is bound INTO the ciphertext so a row lifted across tenants does
  not decrypt. Drop the scope and
  ``test_each_tenant_uses_its_own_credential_and_no_other`` fails.

Plus: round-trip, a wrong or rotated KEK failing closed rather than returning
garbage, a ``key_version`` re-wrap, the audit entry every read appends, both
provider paths receiving the credential, and — the degradation rule — the board
still serving reads with no credential present.

**One acceptance criterion in #364 does not apply to this codebase.** It asks
that the credential never reach argv, because the GitHub provider is
``gh``-CLI-backed with process-ambient auth. That is true of the fleet hub's
canonical provider and NOT of this backend: there is no subprocess here at all
(the only mention of one is a docstring in ``git_providers`` explaining why httpx
is used instead), and both the HTTP GitHub provider and the canonical GitLab one
take an explicit credential argument. Building argv defences for a process this
service never spawns would test nothing. The real leak surface is logs,
exception text, ``__repr__`` and API responses, and that is what is tested here.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest
from cfactory import (
    api_deps,
    auth,
    config,
    credentials,
    git_config,
    git_config_ops,
    github_sync,
    issue_import,
    mcp,
    routes_git_config,
)
from cfactory import (
    cards as cards_module,
)
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.config import Settings
from cfactory.credentials import (
    CredentialError,
    GitCredentialRow,
    KeyRing,
    load_keyring,
    rewrap,
    seal,
    unseal,
)
from cfactory.git_config import (
    CONFIGURED,
    CREDENTIAL_MISSING,
    UNCONFIGURED,
    VERIFIED,
    GitConfigUpdate,
)
from cfactory.git_providers import HttpGitHubProvider, build_provider
from fastapi.testclient import TestClient
from runners.github.providers.gitlab_provider import GitLabProvider
from sqlalchemy import select

# Fake key material, generated once and pinned here so a failure is reproducible.
# Not a secret: it protects nothing but this test module's temp databases.
_KEY_V1 = base64.b64encode(b"k1" * 16).decode()
_KEY_V2 = base64.b64encode(b"k2" * 16).decode()
_KEY_OTHER = base64.b64encode(b"xx" * 16).decode()

_ACTIVE = f"v1:{_KEY_V1}"
_ROTATED = f"v2:{_KEY_V2},v1:{_KEY_V1}"

# The value under test throughout: distinctive enough that a substring scan of a
# response body or a log record cannot match it by accident.
_SECRET = "glpat-CREDENTIAL-UNDER-TEST-9f3c1a"  # noqa: S105 — a fake, not a secret
_OTHER_SECRET = "glpat-OTHER-TENANTS-CREDENTIAL-2b7e"  # noqa: S105 — a fake, not a secret

_WRITER = "test-writer-key-not-a-credential"  # noqa: S105 — a fake, not a secret
_READER = "test-reader-key-not-a-credential"  # noqa: S105 — a fake, not a secret
_PROJECT = "acme/widgets"
_TENANT = "default"

_HTTP_FORBIDDEN = 403
_HTTP_UNAVAILABLE = 503


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="credential-test-hmac")  # noqa: S106 — a test fixture, not a secret


@pytest.fixture
def ctx(audit):
    return AuditContext(audit, "tester")


class FakeHost:
    """A stand-in git host recording every request, including its auth header."""

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"message": "denied"})
        return httpx.Response(200, json={"full_name": _PROJECT, "default_branch": "main"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def auth_headers(self) -> list[str]:
        """Every credential-bearing header value the host was sent."""
        return [
            value
            for request in self.requests
            for name, value in request.headers.items()
            if name.lower() in {"authorization", "private-token"}
        ]


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def settings():
    """A deployment with an encryption key and NO environment git credential, so
    what these tests exercise is the tenant's own stored one."""
    return Settings(credential_key=_ACTIVE, github_api_url="https://gh.test")


@pytest.fixture(autouse=True)
def _settings(monkeypatch, settings):
    for module in (cards_module, git_config, git_config_ops, github_sync, issue_import):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def keyring(settings):
    return load_keyring(settings)


@pytest.fixture
def client(cards, audit, host, monkeypatch, _settings):
    """One TestClient serving BOTH surfaces over one store (RFC-0019 §3.3)."""
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", host.transport)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.set_keys({_WRITER: {"read", "write"}, _READER: {"read"}})

    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = host.transport
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.reset_keystore()


def _config_url(tenant: str = _TENANT) -> str:
    return f"/api/tenants/{tenant}/git-config"


def _url(tenant: str = _TENANT) -> str:
    return f"/api/tenants/{tenant}/git-credential"


def _call_tool(client, name: str, arguments: dict | None = None, key: str = _WRITER):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )


def _tool_payload(client, name: str, arguments: dict | None = None) -> dict:
    resp = _call_tool(client, name, arguments)
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


# ── the crypto, without a database ───────────────────────────────────────────


def test_a_sealed_credential_round_trips(keyring):
    sealed = seal(_SECRET, tenant=_TENANT, keyring=keyring)

    assert unseal(sealed, tenant=_TENANT, keyring=keyring) == _SECRET


def test_two_seals_of_the_same_value_differ(keyring):
    """Each record gets its own data key and its own nonce, so identical
    credentials do not produce identical ciphertext — otherwise the store would
    leak "these two tenants use the same token" to anyone reading the table."""
    first = seal(_SECRET, tenant=_TENANT, keyring=keyring)
    second = seal(_SECRET, tenant=_TENANT, keyring=keyring)

    assert first.ciphertext != second.ciphertext
    assert first.wrapped_key != second.wrapped_key


def test_a_different_key_fails_closed_rather_than_returning_garbage(keyring):
    """AES-GCM authenticates: a wrong key is an error, never a plausible string.
    That is the property an unauthenticated mode would not give us."""
    other = load_keyring(Settings(credential_key=f"v1:{_KEY_OTHER}"))
    sealed = seal(_SECRET, tenant=_TENANT, keyring=keyring)

    with pytest.raises(CredentialError, match="did not decrypt"):
        unseal(sealed, tenant=_TENANT, keyring=other)


def test_a_key_this_process_does_not_hold_says_so(keyring):
    sealed = seal(_SECRET, tenant=_TENANT, keyring=keyring)
    rotated_away = load_keyring(Settings(credential_key=f"v2:{_KEY_V2}"))

    with pytest.raises(CredentialError, match="does not hold key 'v1'"):
        unseal(sealed, tenant=_TENANT, keyring=rotated_away)


def test_a_tampered_record_does_not_decrypt(keyring):
    sealed = seal(_SECRET, tenant=_TENANT, keyring=keyring)
    altered = credentials.Sealed(
        sealed.key_version, sealed.wrapped_key, sealed.ciphertext[:-1] + b"\x00"
    )

    with pytest.raises(CredentialError, match="did not decrypt"):
        unseal(altered, tenant=_TENANT, keyring=keyring)


def test_a_record_lifted_into_another_tenant_does_not_decrypt(keyring):
    """The tenant is associated data on BOTH layers, so isolation survives a
    wrong WHERE clause — this is the guard for when the scope is the bug."""
    sealed = seal(_SECRET, tenant="acme", keyring=keyring)

    with pytest.raises(CredentialError, match="did not decrypt"):
        unseal(sealed, tenant="globex", keyring=keyring)


def test_rewrapping_moves_the_key_without_decrypting_the_credential(keyring):
    sealed = seal(_SECRET, tenant=_TENANT, keyring=keyring)
    rotated = load_keyring(Settings(credential_key=_ROTATED))

    moved = rewrap(sealed, tenant=_TENANT, keyring=rotated)

    assert moved is not None
    assert moved.key_version == "v2"
    # The payload is byte-identical: only the data key changed keys.
    assert moved.ciphertext == sealed.ciphertext
    assert moved.wrapped_key != sealed.wrapped_key
    assert unseal(moved, tenant=_TENANT, keyring=rotated) == _SECRET
    # And it is readable with ONLY the new key, which is what lets the old one
    # be dropped from the environment.
    assert unseal(moved, tenant=_TENANT, keyring=KeyRing(rotated.keys[:1])) == _SECRET


def test_rewrapping_a_record_already_on_the_active_key_is_a_no_op(keyring):
    sealed = seal(_SECRET, tenant=_TENANT, keyring=keyring)

    assert rewrap(sealed, tenant=_TENANT, keyring=keyring) is None


# ── the key ring, and refusing to guess ──────────────────────────────────────


def test_no_key_configured_is_not_an_error_it_is_no_key():
    assert load_keyring(Settings()) is None


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("v1:not-base64!!", "not valid base64"),
        (f"v1:{base64.b64encode(b'too short').decode()}", "AES-256-GCM needs exactly 32"),
        (f"v1:{_KEY_V1},v1:{_KEY_V2}", "same key id twice"),
    ],
)
def test_an_unusable_key_is_refused_rather_than_stretched(spec, expected):
    """A passphrase, a truncated value or a typo must not be silently hashed into
    something key-shaped: that turns a mistake into a weaker key nobody notices.
    The error says how to generate a real one."""
    with pytest.raises(CredentialError, match=expected):
        load_keyring(Settings(credential_key=spec))


def test_bare_key_material_is_read_as_the_first_version():
    """The single-key deployment is the common one, so it need not invent an id."""
    keyring = load_keyring(Settings(credential_key=_KEY_V1))

    assert keyring is not None
    assert keyring.active[0] == "v1"


def test_the_keyring_never_renders_key_material():
    keyring = load_keyring(Settings(credential_key=_ROTATED))

    assert repr(keyring) == "KeyRing(ids=['v2', 'v1'])"
    assert _KEY_V1 not in repr(keyring)


# ── storing: encrypted, or not at all ────────────────────────────────────────


def test_storing_a_credential_round_trips_through_the_store(cards, settings):
    cards.set_git_credential(_SECRET)

    assert cards.git_credential(settings).token() == _SECRET


def test_nothing_stored_anywhere_contains_the_credential(cards, settings):
    """MUTATION GUARD (encryption): store the plaintext instead of sealing it and
    this fails. Every column is checked, not only the one meant to hold it."""
    cards.set_git_credential(_SECRET)

    row = cards.git_credential_row()
    stored = [getattr(row, column.name) for column in row.__table__.columns]
    for value in stored:
        blob = value if isinstance(value, bytes) else str(value).encode()
        assert _SECRET.encode() not in blob
        assert base64.b64encode(_SECRET.encode()) not in blob
    assert row.ciphertext != _SECRET.encode()
    assert row.key_version == "v1"


def test_storing_without_an_encryption_key_is_refused_and_writes_nothing(cards, settings):
    """FAIL CLOSED. No key means no credential — never a plaintext fallback."""
    settings.credential_key = None

    with pytest.raises(CredentialError, match="CFACTORY_CREDENTIAL_KEY is not set"):
        cards.set_git_credential(_SECRET)
    assert cards.git_credential_row() is None


def test_an_empty_credential_is_refused(cards):
    with pytest.raises(CredentialError, match="must not be empty"):
        cards.set_git_credential("   ")


def test_replacing_a_credential_updates_the_row_rather_than_adding_one(cards, settings):
    """One credential per tenant. A second row would mean a revoked credential
    still sitting in the table behind the current one."""
    cards.set_git_credential(_SECRET)
    first = cards.git_credential_row().id

    cards.set_git_credential(_OTHER_SECRET)

    assert cards.git_credential_row().id == first
    assert cards.git_credential(settings).token() == _OTHER_SECRET


def test_clearing_forgets_it_and_is_idempotent(cards, settings):
    cards.set_git_credential(_SECRET)

    assert cards.clear_git_credential() is True
    assert cards.clear_git_credential() is False
    assert cards.git_credential(settings).configured is False
    assert cards.git_credential(settings).token() is None


# ── reading: audited, fail-closed, re-wrapping ───────────────────────────────


def test_every_credential_read_is_audit_chained(cards, audit, settings):
    cards.set_git_credential(_SECRET)

    cards.git_credential(settings, actor="tester", audit=audit).token()

    entries = [(e.kind, e.actor, e.ok, e.correlation_key) for e in audit.list()]
    assert ("read_git_credential", "tester", True, f"tenant:{_TENANT}") in entries
    assert audit.verify() == [], "the tamper-evident chain must still be intact"


def test_a_failed_read_is_audited_too(cards, audit, settings):
    """"The credential could not be read at 14:02" is exactly the entry an
    operator needs after a key rotation goes wrong, so failures chain as well."""
    cards.set_git_credential(_SECRET)
    settings.credential_key = f"v1:{_KEY_OTHER}"

    assert cards.git_credential(settings, audit=audit).token() is None

    assert [(e.kind, e.ok) for e in audit.list()] == [("read_git_credential", False)]


def test_a_lost_key_yields_no_credential_rather_than_garbage(cards, settings):
    cards.set_git_credential(_SECRET)
    settings.credential_key = None

    credential = cards.git_credential(settings)

    assert credential.configured is False
    assert credential.token() is None


def test_a_read_rewraps_a_record_onto_the_active_key(cards, settings, audit):
    cards.set_git_credential(_SECRET)
    assert cards.git_credential_row().key_version == "v1"

    settings.credential_key = _ROTATED
    assert cards.git_credential(settings, audit=audit).token() == _SECRET

    assert cards.git_credential_row().key_version == "v2"
    # And the old key can now be dropped: the record reads with v2 alone.
    settings.credential_key = f"v2:{_KEY_V2}"
    assert cards.git_credential(settings, audit=audit).token() == _SECRET


def test_reading_does_not_fall_back_to_the_deployment_credential(cards, settings):
    """A tenant that stored its own credential and cannot currently read it gets
    NOTHING — handing it the operator's environment token instead would be the
    cross-tenant leak this whole phase closes."""
    settings.git_provider_token = "env-token-not-a-credential"  # noqa: S105 — a fake
    cards.set_git_credential(_SECRET)
    settings.credential_key = None

    assert cards.git_credential(settings).token() is None


def test_a_tenant_with_no_stored_credential_still_uses_the_environment_one(cards, settings):
    """The pre-phase-3 deployment keeps working untouched."""
    settings.git_provider_token = "env-token-not-a-credential"  # noqa: S105 — a fake

    credential = cards.git_credential(settings)

    assert credential.configured is True
    assert credential.info.source == "env"
    assert credential.token() == "env-token-not-a-credential"


def test_resolving_a_target_does_not_read_the_credential(cards, audit, settings):
    """The panel resolves a target on every poll. If that decrypted anything, a
    dashboard left open would fill the audit chain and keep a credential in
    memory to render a boolean."""
    cards.set_git_credential(_SECRET)

    cards.git_target(settings, audit=audit)

    assert audit.list() == []


# ── the credential is never returned, never logged ───────────────────────────


def test_no_surface_ever_returns_the_credential(client, host):
    """MUTATION GUARD (redaction): let the credential into any response body and
    this fails. Every surface that touches it is swept, not only the obvious one."""
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})

    bodies = [
        client.put(_url(), json={"credential": _SECRET}).text,
        client.get(_config_url()).text,
        client.post(f"{_config_url()}:verify").text,
        client.get("/api/cards").text,
        json.dumps(_tool_payload(client, "cfactory_get_git_config")),
        json.dumps(_tool_payload(client, "cfactory_set_git_credential", {"credential": _SECRET})),
        json.dumps(_tool_payload(client, "cfactory_delete_git_credential")),
        client.get("/openapi.json").text,
        client.get("/.well-known/agent-skills/index.json").text,
    ]

    for body in bodies:
        assert _SECRET not in body


def test_the_masked_indicator_says_whether_not_which(client):
    client.put(_url(), json={"credential": _SECRET})

    credential = client.get(_config_url()).json()["credential"]

    assert credential["configured"] is True
    assert credential["source"] == "tenant"
    assert credential["key_version"] == "v1"
    assert credential["updated_at"]
    # Not even a masked prefix: a stored last-four is still a stored fragment.
    assert set(credential) == {"configured", "source", "updated_at", "key_version"}


def test_nothing_is_ever_logged_that_contains_the_credential(client, caplog, host):
    """MUTATION GUARD (redaction, second half): log the credential anywhere on
    the store -> use -> fail path and this fails."""
    caplog.set_level(logging.DEBUG)
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})
    client.put(_url(), json={"credential": _SECRET})
    client.post(f"{_config_url()}:verify")
    # ... and the failure paths, which are where a secret usually escapes.
    host.status_code = 401
    client.post(f"{_config_url()}:verify")
    client.put(_url(), json={"credential": ""})

    for record in caplog.records:
        assert _SECRET not in record.getMessage()
        assert _SECRET not in str(record.args or "")


def test_no_audit_entry_carries_the_credential(client, audit):
    client.put(_url(), json={"credential": _SECRET})

    for entry in audit.list():
        assert _SECRET not in entry.model_dump_json()


def test_the_provider_does_not_render_its_credential():
    """A dataclass prints every field by default, which for a token-carrying
    object is one ``logger.debug("provider=%s", provider)`` away from a leak."""
    provider = HttpGitHubProvider(_PROJECT, _SECRET)

    assert _SECRET not in repr(provider)


def test_the_credential_handle_does_not_render_the_credential(cards, settings):
    cards.set_git_credential(_SECRET)

    assert _SECRET not in repr(cards.git_credential(settings))
    assert _SECRET not in repr(cards.git_credential_row().sealed())


def test_storing_without_a_key_says_why_without_quoting_the_credential(client, settings):
    settings.credential_key = None

    resp = client.put(_url(), json={"credential": _SECRET})

    assert resp.status_code == _HTTP_UNAVAILABLE
    assert "CFACTORY_CREDENTIAL_KEY" in resp.json()["detail"]
    assert _SECRET not in resp.text


# ── injection into the providers, per invocation ─────────────────────────────


def test_the_github_provider_is_handed_the_tenants_credential(cards, settings, host):
    cards.set_git_credential(_SECRET)
    cards.set_git_config(GitConfigUpdate(provider="github", project=_PROJECT))

    target = cards.git_target(settings)
    provider = build_provider(target, _PROJECT, transport=host.transport())

    assert isinstance(provider, HttpGitHubProvider)
    assert provider._token == _SECRET  # noqa: SLF001 — the injection IS the assertion


def test_the_gitlab_provider_is_handed_the_tenants_credential(cards, settings):
    """Both provider paths, because #364 asks for both — and because GitLab goes
    through the fleet's canonical provider rather than our own class."""
    cards.set_git_credential(_SECRET)
    cards.set_git_config(GitConfigUpdate(provider="gitlab", project="acme/group/widgets"))

    provider = build_provider(cards.git_target(settings), "acme/group/widgets")

    assert isinstance(provider, GitLabProvider)
    assert provider._token == _SECRET  # noqa: SLF001 — the injection IS the assertion


def test_the_credential_reaches_the_host_on_a_real_call(cards, ctx, settings, host):
    cards.set_git_credential(_SECRET)
    cards.set_git_config(GitConfigUpdate(provider="github", project=_PROJECT))

    git_config_ops.verify_git_config(cards, ctx, transport=host.transport())

    assert host.auth_headers() == [f"Bearer {_SECRET}"]


def test_the_credential_is_fetched_once_per_provider_build(cards, settings, audit):
    """Injected PER INVOCATION: two provider builds are two audited fetches, and
    a target held between them carries no credential of its own."""
    cards.set_git_credential(_SECRET)
    target = cards.git_target(settings, audit=audit)

    build_provider(target, _PROJECT)
    build_provider(target, _PROJECT)

    assert [e.kind for e in audit.list()] == ["read_git_credential"] * 2


def test_a_card_sync_opens_its_issue_with_the_tenants_credential(cards, ctx, settings, host):
    cards.set_git_credential(_SECRET)
    cards.set_git_config(GitConfigUpdate(provider="github", project=_PROJECT))
    card = cards.create(CardCreate(title="planned", status="ready"))

    github_sync.sync_card(cards, card, settings=settings, transport=host.transport())

    assert host.auth_headers() == [f"Bearer {_SECRET}"]
    # And the sync path's read is chained too, on the store's own audit chain.
    assert [e.kind for e in cards.audit_store().list()] == ["read_git_credential"]


# ── status, and the board that keeps serving ─────────────────────────────────


def test_status_becomes_credential_missing_without_one_and_recovers_with_one(client):
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})
    assert client.get(_config_url()).json()["status"] == CREDENTIAL_MISSING

    client.put(_url(), json={"credential": _SECRET})
    assert client.get(_config_url()).json()["status"] == CONFIGURED

    client.delete(_url())
    assert client.get(_config_url()).json()["status"] == CREDENTIAL_MISSING


def test_a_rejected_credential_reads_as_missing_not_as_configured(client, host):
    """RFC-0020 §3.4: absent OR rejected. A token the host refuses is, from the
    board's point of view, a token it does not have — and reporting the green
    would be the most misleading answer available."""
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})
    client.put(_url(), json={"credential": _SECRET})
    assert client.post(f"{_config_url()}:verify").json()["ok"] is True
    assert client.get(_config_url()).json()["status"] == VERIFIED

    host.status_code = 401
    assert client.post(f"{_config_url()}:verify").json()["ok"] is False

    assert client.get(_config_url()).json()["status"] == CREDENTIAL_MISSING


def test_storing_a_new_credential_clears_an_earlier_rejection(client, host):
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})
    client.put(_url(), json={"credential": _SECRET})
    host.status_code = 403
    client.post(f"{_config_url()}:verify")

    client.put(_url(), json={"credential": _OTHER_SECRET})

    assert client.get(_config_url()).json()["status"] == CONFIGURED


def test_a_failure_that_is_not_a_rejection_does_not_blame_the_credential(client, host):
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})
    client.put(_url(), json={"credential": _SECRET})
    host.status_code = 500

    client.post(f"{_config_url()}:verify")

    assert client.get(_config_url()).json()["status"] == CONFIGURED


def test_the_board_keeps_serving_reads_with_no_credential(client, cards):
    """A missing credential DEGRADES the board. It must never 500 it."""
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})
    cards.create(CardCreate(title="planned", status="ready"))

    assert client.get("/api/cards").status_code == 200
    assert client.get("/api/cards").json()["count"] == 1
    config_read = client.get(_config_url())
    assert config_read.status_code == 200
    assert config_read.json()["status"] == CREDENTIAL_MISSING
    # Asking explicitly says so plainly, still with a 200 and a reason.
    verify = client.post(f"{_config_url()}:verify")
    assert verify.status_code == 200
    assert verify.json()["ok"] is False
    assert "no credential" in verify.json()["reason"]
    # And the import degrades the same way rather than raising.
    imported = client.post("/api/cards/import")
    assert imported.status_code == 200
    assert imported.json()["ok"] is True


def test_the_board_keeps_serving_when_the_encryption_key_is_lost(client, settings):
    client.put(_config_url(), json={"provider": "github", "project": _PROJECT})
    client.put(_url(), json={"credential": _SECRET})

    settings.credential_key = None

    body = client.get(_config_url())
    assert body.status_code == 200
    assert body.json()["status"] == CREDENTIAL_MISSING
    assert body.json()["credential"]["configured"] is False
    assert client.get("/api/cards").status_code == 200


def test_an_unconfigured_tenant_is_still_unconfigured_with_a_credential(client):
    """No project named outranks everything: a credential with nowhere to point
    it is not a configured board."""
    client.put(_url(), json={"credential": _SECRET})

    assert client.get(_config_url()).json()["status"] == UNCONFIGURED


# ── surfaces: parity, scope, audit ───────────────────────────────────────────


def test_the_mcp_twin_stores_and_clears_the_same_credential(client, cards, settings):
    _tool_payload(client, "cfactory_set_git_credential", {"credential": _SECRET})
    assert cards.git_credential(settings).token() == _SECRET

    payload = _tool_payload(client, "cfactory_delete_git_credential")

    assert payload["removed"] is True
    assert payload["credential"]["configured"] is False


def test_the_mcp_twin_fails_closed_without_a_key(client, settings):
    settings.credential_key = None

    payload = _tool_payload(client, "cfactory_set_git_credential", {"credential": _SECRET})

    assert "CFACTORY_CREDENTIAL_KEY" in payload["error"]
    assert _SECRET not in json.dumps(payload)


def test_there_is_no_tool_that_reads_a_credential(client):
    names = {t["name"] for t in mcp.MCP_TOOLS if "git_credential" in t["name"]}

    assert names == {"cfactory_set_git_credential", "cfactory_delete_git_credential"}
    assert all(mcp.TOOL_SCOPES[name] == auth.WRITE for name in names)


def test_a_read_scoped_key_cannot_store_or_clear_a_credential(client):
    reader = {"Authorization": f"Bearer {_READER}"}

    assert client.put(_url(), json={"credential": _SECRET}, headers=reader).status_code == 403
    assert client.delete(_url(), headers=reader).status_code == 403
    assert (
        _call_tool(client, "cfactory_set_git_credential", {"credential": _SECRET}, key=_READER)
    ).status_code == 403


def test_both_credential_mutations_are_audit_chained(client, audit):
    client.put(_url(), json={"credential": _SECRET})
    client.delete(_url())

    kinds = [(e.kind, e.ok, e.endpoint) for e in audit.list()]
    assert ("set_git_credential", True, f"/api/tenants/{_TENANT}/git-credential") in kinds
    assert ("delete_git_credential", True, f"/api/tenants/{_TENANT}/git-credential") in kinds
    assert audit.verify() == []


# ── tenant isolation (the third mutation-check guard) ────────────────────────


@pytest.fixture
def multi_tenant_client(cards, audit, host, monkeypatch, settings):
    """The same app with CFACTORY_MULTI_TENANT on, so the tenant comes from the
    X-Tenant-Id header oauth2-proxy injects from the Keycloak claim.

    The REAL ``cards_store_dep`` runs here (only the underlying store is a temp
    one), because the thing under test IS the scoping the dependency performs.
    """
    monkeypatch.setattr(settings, "multi_tenant", True)
    monkeypatch.setattr(api_deps, "get_settings", lambda: settings)
    monkeypatch.setattr(api_deps, "get_cards_store", lambda: cards)
    monkeypatch.setattr(routes_git_config, "get_settings", lambda: settings)
    auth.set_keys({_WRITER: {"read", "write"}})

    app = create_app()
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = host.transport
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.reset_keystore()


def _as(client, tenant: str, method: str, path: str, **kwargs):
    return getattr(client, method)(path, headers={"X-Tenant-Id": tenant}, **kwargs)


def test_a_tenant_cannot_store_or_clear_another_tenants_credential(multi_tenant_client):
    client = multi_tenant_client
    _as(client, "acme", "put", _url("acme"), json={"credential": _SECRET})

    assert (
        _as(client, "globex", "put", _url("acme"), json={"credential": _OTHER_SECRET}).status_code
        == _HTTP_FORBIDDEN
    )
    assert _as(client, "globex", "delete", _url("acme")).status_code == _HTTP_FORBIDDEN


def test_each_tenant_uses_its_own_credential_and_no_other(multi_tenant_client, cards, settings):
    """MUTATION GUARD (tenant isolation): drop the tenant filter on the credential
    query and this fails — one tenant would resolve the other's row."""
    client = multi_tenant_client
    _as(client, "acme", "put", _url("acme"), json={"credential": _SECRET})
    _as(client, "globex", "put", _url("globex"), json={"credential": _OTHER_SECRET})

    assert cards.scoped("acme").git_credential(settings).token() == _SECRET
    assert cards.scoped("globex").git_credential(settings).token() == _OTHER_SECRET
    assert cards.scoped("default").git_credential(settings).configured is False


def test_one_tenants_write_does_not_overwrite_anothers(multi_tenant_client, cards, settings):
    client = multi_tenant_client
    _as(client, "acme", "put", _url("acme"), json={"credential": _SECRET})

    _as(client, "globex", "put", _url("globex"), json={"credential": _OTHER_SECRET})

    assert cards.scoped("acme").git_credential(settings).token() == _SECRET
    assert cards.scoped("acme").git_credential_row().id != (
        cards.scoped("globex").git_credential_row().id
    )


def test_clearing_one_tenants_credential_leaves_the_others(multi_tenant_client, cards, settings):
    client = multi_tenant_client
    _as(client, "acme", "put", _url("acme"), json={"credential": _SECRET})
    _as(client, "globex", "put", _url("globex"), json={"credential": _OTHER_SECRET})

    _as(client, "acme", "delete", _url("acme"))

    assert cards.scoped("acme").git_credential(settings).configured is False
    assert cards.scoped("globex").git_credential(settings).token() == _OTHER_SECRET


def test_the_credential_tools_have_no_way_to_name_another_tenant(client):
    """Isolation by construction: an agent operates on its own partition or on
    nothing."""
    tools = [t for t in mcp.MCP_TOOLS if "git_credential" in t["name"]]

    assert tools
    for tool in tools:
        assert "tenant" not in tool["inputSchema"].get("properties", {})

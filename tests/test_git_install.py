"""The GitHub App / GitLab OAuth install flow (RFC-0020 §3.4 phase 4, #365).

Phase 3 pinned the crypto; this pins the two properties phase 4 adds, and both
are mutation-checked — remove the guard and a test here goes red:

* **THE CALLBACK BELIEVES NOTHING WITHOUT A STATE.** Delete the ``state``
  verification and ``test_the_callback_refuses_a_state_it_never_issued``,
  ``test_a_state_cannot_be_replayed``, ``test_an_expired_state_is_refused`` and
  ``test_the_callback_ignores_the_tenant_the_request_claims`` all fail. The state
  is stored as a SHA-256 and consumed inside the transaction that reads it, so
  un-replayability is a property of the delete rather than of a flag someone can
  forget to set.
* **A MINTED TOKEN IS NEVER PERSISTED.** Let one into the phase-3 store and
  ``test_a_minted_installation_token_is_never_persisted``,
  ``test_gitlab_stores_the_refresh_credential_and_never_the_access_one`` and
  ``test_persistable_secret_refuses_a_github_token`` fail. The first two scan the
  database FILE, so a leak through any column or any encoding is caught; the
  third pins the single function that decides it.

Plus the rest of the acceptance criteria: a refresh failure degrading the tenant
to ``credential_missing`` rather than continuing, board writes failing LOUDLY on
a degraded install instead of reporting "not configured", an install attaching to
exactly the connection and tenant it was started for, one tenant being unable to
touch another's, GitLab's rotated refresh credential being written back, and
Azure DevOps having no install flow at all.

**Every provider call is mocked.** Registering a real GitHub App is a human step
this test suite cannot perform, which is the whole reason the App credentials are
deployment configuration — so what is exercised here is the complete code path
against a fake host, with a throwaway RSA key generated in-process.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cfactory import (
    api_deps,
    config,
    git_config,
    git_config_ops,
    git_install,
    github_sync,
    issue_import,
    routes_install,
)
from cfactory import (
    cards as cards_module,
)
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.config import Settings
from cfactory.credentials import GitCredentialRow
from cfactory.git_config import CREDENTIAL_MISSING, GitConfigUpdate
from cfactory.git_connections import (
    GitConnectionCreate,
    GitRepositoryCreate,
    GitResourceNotFoundError,
)
from cfactory.git_install import (
    CALLBACK_PATH,
    GITHUB,
    GITLAB,
    INSTALLED,
    CallbackClaim,
    GitInstallRow,
    InstallError,
    app_jwt,
    install_available,
    persistable_secret,
)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

# Distinctive enough that a substring scan of a database file or a response body
# cannot match one by accident. None of these is real.
_MINTED = "ghs_MINTED-INSTALLATION-TOKEN-4c1f9a"
_GL_ACCESS = "glpat-ACCESS-TOKEN-MUST-NOT-PERSIST-77b2"
_GL_REFRESH = "glrt-REFRESH-CREDENTIAL-MAY-PERSIST-31de"
_GL_REFRESH_2 = "glrt-ROTATED-REFRESH-CREDENTIAL-9a04"

_KEY = base64.b64encode(b"k1" * 16).decode()
_CREDENTIAL_KEY = f"v1:{_KEY}"

_INSTALLATION = "4242"
_ACCOUNT = "acme-org"
_PROJECT = "acme/widgets"
_CALLBACK_BASE = "https://cfactory-mcp.test"

_HTTP_BAD_REQUEST = 400
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404


@pytest.fixture(scope="session")
def app_pem() -> str:
    """A throwaway RSA key standing in for the one GitHub hands a real operator.

    Generated in-process rather than committed: a PEM in the repository looks like
    a real App key to every scanner that reads it, and a fixture that generates
    one cannot be mistaken for a leaked credential.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def settings(app_pem) -> Settings:
    """A deployment with both apps registered and no environment git credential."""
    return Settings(
        credential_key=_CREDENTIAL_KEY,
        install_callback_base_url=_CALLBACK_BASE,
        github_app_id="123456",
        github_app_slug="cfactory-test",
        github_app_private_key=app_pem,
        gitlab_oauth_client_id="gl-client-id",
        gitlab_oauth_client_secret="gl-client-secret",  # noqa: S106 — a fake, not a secret
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch, settings):
    for module in (
        cards_module,
        git_config,
        git_config_ops,
        github_sync,
        issue_import,
        routes_install,
    ):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def _no_cached_tokens():
    """Minted tokens are process-global by design; tests must not inherit them."""
    git_install.clear_token_cache()
    yield
    git_install.clear_token_cache()
    git_install.HTTP_TRANSPORT = None


@pytest.fixture
def db(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'cards.db'}"


@pytest.fixture
def cards(db) -> CardStore:
    return CardStore(db)


@pytest.fixture
def audit(tmp_path) -> AuditStore:
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="install-test-hmac")  # noqa: S106 — a test fixture, not a secret


@pytest.fixture
def ctx(audit) -> AuditContext:
    return AuditContext(audit, "tester")


# ── the fake hosts ───────────────────────────────────────────────────────────


class FakeGitHubApp:
    """GitHub's App endpoints, recording every request it is sent."""

    def __init__(self, *, known_installation: str = _INSTALLATION) -> None:
        self.known = known_installation
        self.requests: list[httpx.Request] = []
        self.mint_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # GitHub Enterprise serves the same API under /api/v3, so a GHES
        # connection's base_url carries that prefix. Stripping it here is what
        # makes one fake host answer for github.com and for a self-hosted one.
        path = request.url.path.removeprefix("/api/v3")
        if path == f"/app/installations/{self.known}" and request.method == "GET":
            return httpx.Response(200, json={"id": int(self.known), "account": {"login": _ACCOUNT}})
        if path.startswith("/app/installations/") and request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"/app/installations/{self.known}/access_tokens":
            if self.mint_status != 200:
                return httpx.Response(self.mint_status, json={"message": "denied"})
            return httpx.Response(
                201,
                json={
                    "token": _MINTED,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        if path.startswith("/repos/"):
            return httpx.Response(200, json={"full_name": _PROJECT})
        return httpx.Response(404, json={"message": "unexpected"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bearers(self) -> list[str]:
        return [r.headers.get("authorization", "") for r in self.requests]


class FakeGitLab:
    """GitLab's ``/oauth/token`` plus enough of the API to verify a connection."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.forms: list[dict[str, str]] = []
        self.refresh_status = 200
        self.next_refresh: str | None = _GL_REFRESH

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.forms.append(form)
            if form.get("grant_type") == "refresh_token" and self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={"error": "invalid_grant"})
            body = {"access_token": _GL_ACCESS, "expires_in": 7200, "token_type": "bearer"}
            if self.next_refresh:
                body["refresh_token"] = self.next_refresh
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"path_with_namespace": _PROJECT})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def gh() -> FakeGitHubApp:
    return FakeGitHubApp()


@pytest.fixture
def gl() -> FakeGitLab:
    return FakeGitLab()


# ── helpers ──────────────────────────────────────────────────────────────────


def _connection(cards: CardStore, provider: str = GITHUB, *, project: str = _PROJECT) -> int:
    """A connection with one repository — the shape an install attaches to."""
    row = cards.create_connection(GitConnectionCreate(provider=provider))
    cards.create_repository(row.id, GitRepositoryCreate(project=project, make_default=True))
    return row.id


def _start(cards: CardStore, ctx: AuditContext, connection_id: int) -> str:
    """Begin an install and recover the state from the URL the caller is given."""
    result = git_config_ops.start_git_install(cards, ctx, connection_id)
    state = httpx.URL(str(result["authorize_url"])).params.get("state")
    assert state
    return str(state)


def _install_github(cards: CardStore, ctx: AuditContext, gh: FakeGitHubApp) -> int:
    connection_id = _connection(cards)
    state = _start(cards, ctx, connection_id)
    git_config_ops.complete_git_install(
        cards,
        CallbackClaim(state, _INSTALLATION, None),
        transport=gh.transport(),
    )
    return connection_id


def _install_gitlab(cards: CardStore, ctx: AuditContext, gl: FakeGitLab) -> int:
    connection_id = _connection(cards, GITLAB, project="acme/gl-widgets")
    state = _start(cards, ctx, connection_id)
    git_config_ops.complete_git_install(
        cards, CallbackClaim(state, None, "gl-code"), transport=gl.transport()
    )
    return connection_id


def _database_bytes(db: str) -> bytes:
    return open(db.replace("sqlite:///", ""), "rb").read()  # noqa: PTH123


# ── the state check (MUTATION GUARD (a)) ─────────────────────────────────────


def test_the_callback_refuses_a_state_it_never_issued(cards, ctx, gh):
    """MUTATION GUARD: drop the state verification and this test fails.

    A LIVE state exists throughout — for a different install, started by the
    legitimate user — because that is the case a weakened check actually gets
    wrong. A callback that matched "any pending state" rather than *this* one
    would accept the forgery here and consume somebody else's state; a callback
    that skipped the check entirely would accept it too. Both are caught by the
    same three assertions: the forgery is refused, no install is recorded, and the
    real state is still there afterwards to be used by the person who started it.

    Nothing is asked of the provider either — the state is checked before the
    round trip, so a forged callback cannot even make this deployment generate
    traffic.
    """
    victim = _connection(cards)
    attacker_target = cards.create_connection(
        GitConnectionCreate(provider=GITHUB, base_url="https://ghe.example.com/api/v3")
    ).id
    cards.create_repository(attacker_target, GitRepositoryCreate(project="acme/enterprise"))
    live_state = _start(cards, ctx, victim)

    with pytest.raises(InstallError):
        git_config_ops.complete_git_install(
            cards,
            CallbackClaim("a-state-nobody-issued", _INSTALLATION, None),
            transport=gh.transport(),
        )

    assert cards.install_row(victim) is None
    assert cards.install_row(attacker_target) is None
    assert gh.requests == []
    # The legitimate state was not spent by the forged attempt.
    git_config_ops.complete_git_install(
        cards, CallbackClaim(live_state, _INSTALLATION, None), transport=gh.transport()
    )
    assert cards.install_row(victim).installation_id == _INSTALLATION


def test_the_callback_refuses_an_empty_state(cards, ctx, gh):
    """Again with a live state present, so "match anything" is not a way through."""
    connection_id = _connection(cards)
    _start(cards, ctx, connection_id)

    with pytest.raises(InstallError):
        git_config_ops.complete_git_install(
            cards, CallbackClaim("", _INSTALLATION, None), transport=gh.transport()
        )
    assert cards.install_row(connection_id) is None


def test_a_state_cannot_be_replayed(cards, ctx, gh):
    """One state, one install. The row is consumed in the transaction that reads it."""
    connection_id = _connection(cards)
    state = _start(cards, ctx, connection_id)

    git_config_ops.complete_git_install(
        cards, CallbackClaim(state, _INSTALLATION, None), transport=gh.transport()
    )
    assert cards.install_row(connection_id) is not None

    with pytest.raises(InstallError):
        git_config_ops.complete_git_install(
            cards, CallbackClaim(state, "9999", None), transport=gh.transport()
        )
    # The FIRST install is untouched: a replay changes nothing, it does not
    # overwrite the installation with whatever the second attempt named.
    assert cards.install_row(connection_id).installation_id == _INSTALLATION


def test_an_expired_state_is_refused(cards, ctx, gh, monkeypatch):
    monkeypatch.setattr(git_install, "STATE_TTL_SECONDS", -1)
    monkeypatch.setattr(
        cards_module, "state_expiry", lambda: datetime.now(UTC) - timedelta(minutes=1)
    )
    connection_id = _connection(cards)
    state = _start(cards, ctx, connection_id)

    with pytest.raises(InstallError):
        git_config_ops.complete_git_install(
            cards, CallbackClaim(state, _INSTALLATION, None), transport=gh.transport()
        )
    assert cards.install_row(connection_id) is None


def test_the_state_token_itself_is_never_stored(cards, ctx):
    """Only its SHA-256 is. A database read yields nothing presentable."""
    connection_id = _connection(cards)
    state = _start(cards, ctx, connection_id)
    assert state.encode() not in _database_bytes(cards._url)


# ── tenant binding ───────────────────────────────────────────────────────────


def test_the_callback_ignores_the_tenant_the_request_claims(db, ctx, gh):
    """MUTATION GUARD: the tenant comes from the state row, never the request.

    The callback host is not behind oauth2-proxy, so an ``X-Tenant-Id`` on it is a
    claim the browser made. The install lands on the tenant that STARTED it even
    when the store handed to the callback is scoped to a different one.
    """
    alice = CardStore(db).scoped("alice")
    bob = CardStore(db).scoped("bob")
    connection_id = _connection(alice)
    _connection(bob, project="bob/other")
    state = _start(alice, ctx, connection_id)

    # Completed through BOB's scoped store, carrying ALICE's state.
    completed = git_config_ops.complete_git_install(
        bob, CallbackClaim(state, _INSTALLATION, None), transport=gh.transport()
    )

    assert completed.tenant == "alice"
    assert alice.install_row(connection_id) is not None
    assert bob.install_row(bob.connections()[0].id) is None


def test_one_tenants_state_completes_only_that_tenants_install(db, ctx, gh):
    """Two live states, and each one lands where it belongs.

    The pair that a "match any pending state" implementation gets wrong: with
    both tenants mid-install, presenting Bob's state must complete BOB's install
    and leave Alice's pending — not whichever row the query happened to return
    first.
    """
    alice = CardStore(db).scoped("alice")
    bob = CardStore(db).scoped("bob")
    alice_connection = _connection(alice)
    bob_connection = _connection(bob, project="bob/other")
    _start(alice, ctx, alice_connection)
    bob_state = _start(bob, ctx, bob_connection)

    completed = git_config_ops.complete_git_install(
        CardStore(db),
        CallbackClaim(bob_state, _INSTALLATION, None),
        transport=gh.transport(),
    )

    assert completed.tenant == "bob"
    assert bob.install_row(bob_connection) is not None
    assert alice.install_row(alice_connection) is None


def test_a_tenant_cannot_start_an_install_on_another_tenants_connection(db, ctx):
    """Not found, not forbidden: a 403 would confirm the connection exists."""
    alice = CardStore(db).scoped("alice")
    bob = CardStore(db).scoped("bob")
    alice_connection = _connection(alice)

    with pytest.raises(GitResourceNotFoundError):
        git_config_ops.start_git_install(bob, ctx, alice_connection)


def test_an_install_attaches_to_the_named_connection_only(cards, ctx, gh):
    first = _connection(cards)
    second = cards.create_connection(
        GitConnectionCreate(provider=GITHUB, base_url="https://ghe.example.com/api/v3")
    ).id
    cards.create_repository(second, GitRepositoryCreate(project="acme/enterprise"))

    state = _start(cards, ctx, second)
    git_config_ops.complete_git_install(
        cards, CallbackClaim(state, _INSTALLATION, None), transport=gh.transport()
    )

    assert cards.install_row(first) is None
    assert cards.install_row(second).installation_id == _INSTALLATION


# ── nothing minted is persisted (MUTATION GUARD (b)) ─────────────────────────


def test_a_minted_installation_token_is_never_persisted(cards, ctx, gh, db, settings):
    """MUTATION GUARD: store a minted installation token and this test fails.

    The whole GitHub argument in one assertion. What the tenant keeps is an
    ``installation_id``; the token is minted, handed to one provider call and
    forgotten. The scan is of the database FILE, so a leak through any column, in
    any encoding, is caught — not only one through the credential table.
    """
    connection_id = _install_github(cards, ctx, gh)
    git_install.HTTP_TRANSPORT = gh.transport()

    assert cards.connection_credential(connection_id, settings).token() == _MINTED

    assert cards.credential_row(connection_id) is None
    assert _MINTED.encode() not in _database_bytes(db)


def test_gitlab_stores_the_refresh_credential_and_never_the_access_one(cards, ctx, gl, db):
    """MUTATION GUARD: persist the access token and this test fails.

    GitLab has no App identity, so something long-lived is unavoidable — and it is
    exactly one thing. The refresh credential is sealed by the phase-3 envelope
    (so it is not in the file either, and its absence proves only that it is
    encrypted); the access token is not stored in any form.
    """
    connection_id = _install_gitlab(cards, ctx, gl)

    # Sealed, so present as a row and absent as bytes.
    assert cards.credential_row(connection_id) is not None
    assert _GL_REFRESH.encode() not in _database_bytes(db)
    # The access token has no row of its own and appears nowhere at all.
    assert _GL_ACCESS.encode() not in _database_bytes(db)


def test_persistable_secret_refuses_a_github_token():
    """The single function that decides what an install may write down."""
    tokens = git_install.OAuthTokens(_GL_ACCESS, _GL_REFRESH)

    with pytest.raises(InstallError):
        persistable_secret(GITHUB, tokens)

    assert persistable_secret(GITLAB, tokens) == _GL_REFRESH
    assert persistable_secret(GITLAB, tokens) != _GL_ACCESS


def test_persistable_secret_refuses_a_gitlab_grant_with_no_refresh():
    """No refresh credential means the install dies in two hours. Refuse it now."""
    with pytest.raises(InstallError):
        persistable_secret(GITLAB, git_install.OAuthTokens(_GL_ACCESS, None))


def test_only_one_credential_row_exists_after_a_gitlab_install(cards, ctx, gl):
    """The install flow gets no second credential table and no second crypto path."""
    _install_gitlab(cards, ctx, gl)
    with cards._session() as session:
        assert len(list(session.scalars(select(GitCredentialRow)))) == 1


# ── minting ──────────────────────────────────────────────────────────────────


def test_the_installation_is_verified_against_the_app_before_anything_is_stored(cards, ctx, gh):
    """GitHub's setup redirect carries no signature, so the id is proved by use."""
    connection_id = _connection(cards)
    state = _start(cards, ctx, connection_id)

    with pytest.raises(InstallError):
        git_config_ops.complete_git_install(
            cards,
            # An installation of somebody else's App.
            CallbackClaim(state, "999999", None),
            transport=gh.transport(),
        )
    assert cards.install_row(connection_id) is None


def test_the_verification_presents_an_assertion_signed_by_the_app_key(cards, ctx, gh, app_pem):
    """The only way to ask GitHub about an installation is to hold the App's key."""
    _install_github(cards, ctx, gh)

    bearer = next(b for b in gh.bearers() if b.startswith("Bearer "))
    header, payload, signature = bearer.removeprefix("Bearer ").split(".")
    key = serialization.load_pem_private_key(app_pem.encode(), password=None)
    key.public_key().verify(
        _b64url_decode(signature),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert json.loads(_b64url_decode(payload))["iss"] == "123456"


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def test_the_minted_token_reaches_the_provider(cards, ctx, gh, settings):
    """End to end: an installed connection authenticates a real provider call."""
    _install_github(cards, ctx, gh)
    git_install.HTTP_TRANSPORT = gh.transport()

    result = git_config_ops.verify_git_connection(
        cards, ctx, cards.connections()[0].id, transport=gh.transport()
    )

    assert result["ok"] is True
    assert f"Bearer {_MINTED}" in gh.bearers()


def test_a_token_is_minted_once_and_reused_while_it_is_fresh(cards, ctx, gh, settings):
    connection_id = _install_github(cards, ctx, gh)
    git_install.HTTP_TRANSPORT = gh.transport()

    first = cards.connection_credential(connection_id, settings).token()
    second = cards.connection_credential(connection_id, settings).token()

    assert first == second == _MINTED
    mints = [r for r in gh.requests if r.url.path.endswith("/access_tokens")]
    assert len(mints) == 1


def test_a_gitlab_access_token_is_obtained_by_refreshing(cards, ctx, gl, settings):
    connection_id = _install_gitlab(cards, ctx, gl)
    git_install.HTTP_TRANSPORT = gl.transport()

    assert cards.connection_credential(connection_id, settings).token() == _GL_ACCESS
    assert [f["grant_type"] for f in gl.forms] == ["authorization_code", "refresh_token"]


def test_a_rotated_refresh_credential_is_written_back(cards, ctx, gl, settings):
    """GitLab spends the refresh credential on every exchange. Keep the new one."""
    connection_id = _install_gitlab(cards, ctx, gl)
    before = cards.credential_row(connection_id).ciphertext
    gl.next_refresh = _GL_REFRESH_2
    git_install.HTTP_TRANSPORT = gl.transport()

    cards.connection_credential(connection_id, settings).token()

    assert cards.credential_row(connection_id).ciphertext != before
    # The next refresh presents the ROTATED one, not the spent original.
    git_install.clear_token_cache()
    cards.connection_credential(connection_id, settings).token()
    assert gl.forms[-1]["refresh_token"] == _GL_REFRESH_2


# ── refresh failure degrades, loudly ─────────────────────────────────────────


def test_a_refresh_failure_degrades_the_tenant_to_credential_missing(cards, ctx, gl, settings):
    """The acceptance criterion: a failed refresh does not silently continue."""
    connection_id = _install_gitlab(cards, ctx, gl)
    gl.refresh_status = 401
    git_install.HTTP_TRANSPORT = gl.transport()

    with pytest.raises(InstallError):
        cards.connection_credential(connection_id, settings).token()

    install = cards.install_row(connection_id)
    assert install.status == CREDENTIAL_MISSING
    assert install.error
    # And the connection the panel renders says so, without minting again.
    payload = git_config_ops.list_git_connections(cards, settings)
    connection = payload["connections"][0]
    assert connection["status"] == CREDENTIAL_MISSING
    assert connection["install"]["status"] == CREDENTIAL_MISSING
    assert connection["install"]["error"]
    assert connection["credential"]["configured"] is False


def test_a_mint_failure_degrades_a_github_install_too(cards, ctx, gh, settings):
    connection_id = _install_github(cards, ctx, gh)
    gh.mint_status = 401
    git_install.HTTP_TRANSPORT = gh.transport()

    with pytest.raises(InstallError):
        cards.connection_credential(connection_id, settings).token()

    assert cards.install_row(connection_id).status == CREDENTIAL_MISSING


def test_a_later_success_clears_the_degradation(cards, ctx, gl, settings):
    """A recovered connection must stop reporting an error nobody can clear."""
    connection_id = _install_gitlab(cards, ctx, gl)
    gl.refresh_status = 401
    git_install.HTTP_TRANSPORT = gl.transport()
    with pytest.raises(InstallError):
        cards.connection_credential(connection_id, settings).token()

    gl.refresh_status = 200
    assert cards.connection_credential(connection_id, settings).token() == _GL_ACCESS

    install = cards.install_row(connection_id)
    assert install.status == INSTALLED
    assert install.error is None


def test_a_board_write_on_a_degraded_install_fails_loudly(cards, ctx, gh, settings):
    """LOUD, not silent: the card carries the reason and the caller gets ok=False.

    Without the ``installed`` branch in ``sync_enabled`` this would report
    "github sync not configured" with ``ok=True`` — a write that silently does
    nothing and blames the user for a setup they did perform.
    """
    _install_github(cards, ctx, gh)
    gh.mint_status = 401
    git_install.HTTP_TRANSPORT = gh.transport()
    card = cards.create(CardCreate(title="ships nothing"))

    result = github_sync.sync_card(cards, card, settings=settings, transport=gh.transport())

    assert result["ok"] is False
    assert result["error"]
    assert cards.get(card.card_key).github_sync_error


def test_an_import_on_a_degraded_install_reports_the_reason(cards, ctx, gh, settings):
    """The background poll's never-raises contract holds, and says why."""
    _install_github(cards, ctx, gh)
    gh.mint_status = 401
    git_install.HTTP_TRANSPORT = gh.transport()

    result = issue_import.import_issues(cards, settings=settings, transport=gh.transport())

    assert result["ok"] is False
    assert result["reason"]


# ── what the deployment does and does not offer ──────────────────────────────


def test_azure_devops_has_no_install_flow(cards, ctx):
    """Explicitly out of scope (RFC-0020 §3.4): it keeps the phase-3 path."""
    connection_id = cards.create_connection(GitConnectionCreate(provider="azure_devops")).id

    with pytest.raises(InstallError, match="no install flow"):
        git_config_ops.start_git_install(cards, ctx, connection_id)


def test_install_available_reports_only_what_an_operator_registered(settings, app_pem):
    assert install_available(settings) == {GITHUB: True, GITLAB: True}
    # A deployment that has registered nothing offers nothing — the panel keeps
    # the paste box rather than a button that cannot work.
    assert install_available(Settings()) == {GITHUB: False, GITLAB: False}
    # And no callback URL disables both, whatever else is set: a state bound to a
    # redirect the provider cannot reach is a dead end found out too late.
    no_callback = Settings(github_app_id="1", github_app_slug="s", github_app_private_key=app_pem)
    assert install_available(no_callback) == {GITHUB: False, GITLAB: False}


def test_starting_an_install_is_refused_when_no_app_is_registered(cards, ctx, monkeypatch):
    bare = Settings(credential_key=_CREDENTIAL_KEY)
    monkeypatch.setattr(git_config_ops, "get_settings", lambda: bare)
    monkeypatch.setattr(cards_module, "get_settings", lambda: bare)
    connection_id = _connection(cards)

    with pytest.raises(InstallError, match="no github app registered"):
        git_config_ops.start_git_install(cards, ctx, connection_id)


def test_the_authorize_url_is_the_apps_repository_picker(cards, ctx):
    """The screen where a human chooses which repositories the App may see."""
    result = git_config_ops.start_git_install(cards, ctx, _connection(cards))
    url = httpx.URL(str(result["authorize_url"]))

    assert str(url).startswith("https://github.com/apps/cfactory-test/installations/new")
    assert url.params.get("state")
    assert result["redirect_uri"] == f"{_CALLBACK_BASE}{CALLBACK_PATH}"


def test_the_authorize_url_follows_a_self_hosted_host(cards, ctx):
    """A GitHub Enterprise connection installs on ITS host, not on github.com."""
    connection_id = cards.create_connection(
        GitConnectionCreate(provider=GITHUB, base_url="https://ghe.example.com/api/v3")
    ).id
    result = git_config_ops.start_git_install(cards, ctx, connection_id)

    assert str(result["authorize_url"]).startswith("https://ghe.example.com/apps/cfactory-test/")


def test_the_gitlab_authorize_url_carries_the_registered_redirect(cards, ctx):
    connection_id = cards.create_connection(GitConnectionCreate(provider=GITLAB)).id
    url = httpx.URL(
        str(git_config_ops.start_git_install(cards, ctx, connection_id)["authorize_url"])
    )

    assert url.path == "/oauth/authorize"
    assert url.params.get("redirect_uri") == f"{_CALLBACK_BASE}{CALLBACK_PATH}"
    assert url.params.get("response_type") == "code"
    assert url.params.get("client_id") == "gl-client-id"
    # The client SECRET is never in a URL a browser follows.
    assert "gl-client-secret" not in str(url)


def test_the_app_jwt_is_refused_when_the_key_is_not_a_pem(settings):
    settings.github_app_private_key = "not a pem"
    with pytest.raises(InstallError, match="not a usable PEM"):
        app_jwt(settings)


# ── disconnecting ────────────────────────────────────────────────────────────


def test_disconnecting_forgets_the_installation_and_the_stored_credential(cards, ctx, gl, settings):
    connection_id = _install_gitlab(cards, ctx, gl)

    result = git_config_ops.delete_git_install(cards, ctx, connection_id)

    assert result["removed"] is True
    assert cards.install_row(connection_id) is None
    assert cards.credential_row(connection_id) is None
    # And it says plainly that it did not revoke anything at the provider.
    assert "provider" in str(result["note"])


def test_disconnecting_is_idempotent(cards, ctx):
    connection_id = _connection(cards)
    assert git_config_ops.delete_git_install(cards, ctx, connection_id)["removed"] is False


def test_deleting_the_connection_takes_its_install_with_it(cards, ctx, gh):
    connection_id = _install_github(cards, ctx, gh)
    cards.delete_connection(connection_id)

    with cards._session() as session:
        assert list(session.scalars(select(GitInstallRow))) == []


def test_a_reinstall_replaces_the_previous_one(cards, ctx, gh):
    """Reconnecting after revoking at the provider is the normal repair."""
    connection_id = _install_github(cards, ctx, gh)
    gh.known = "7777"
    state = _start(cards, ctx, connection_id)

    git_config_ops.complete_git_install(
        cards, CallbackClaim(state, "7777", None), transport=gh.transport()
    )

    assert cards.install_row(connection_id).installation_id == "7777"


# ── the HTTP surface ─────────────────────────────────────────────────────────


@pytest.fixture
def client(cards, audit, gh):
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[api_deps.action_transport_dep] = lambda: gh.transport()
    return TestClient(app)


def test_the_callback_is_reachable_without_any_credential(client, cards, ctx, gh):
    """It has to be: a provider redirect carries no API key and no session."""
    connection_id = _connection(cards)
    state = _start(cards, ctx, connection_id)

    response = client.get(CALLBACK_PATH, params={"state": state, "installation_id": _INSTALLATION})

    assert response.status_code == 200
    assert cards.install_row(connection_id) is not None


def test_the_callback_returns_400_and_no_detail_for_a_bad_state(client, cards):
    _connection(cards)
    response = client.get(CALLBACK_PATH, params={"state": "nope", "installation_id": "1"})

    assert response.status_code == _HTTP_BAD_REQUEST
    assert cards.install_row(cards.connections()[0].id) is None


def test_the_callback_never_renders_anything_secret(client, cards, ctx, gh, app_pem):
    connection_id = _connection(cards)
    state = _start(cards, ctx, connection_id)
    body = client.get(CALLBACK_PATH, params={"state": state, "installation_id": _INSTALLATION}).text

    for leak in (_MINTED, state, app_pem.splitlines()[1], "gl-client-secret"):
        assert leak not in body


def test_a_provider_cancellation_changes_nothing(client, cards, ctx):
    connection_id = _connection(cards)
    _start(cards, ctx, connection_id)

    response = client.get(CALLBACK_PATH, params={"error": "access_denied"})

    assert response.status_code == 200
    assert cards.install_row(connection_id) is None


def test_the_callback_is_absent_from_the_published_contract(client):
    """Not part of the board's programmatic surface: it is a browser landing spot.

    Also what keeps tests/test_board_parity.py honest — the callback has no MCP
    twin by design, and it is outside /api/ so the parity sweep never claims it.
    """
    assert CALLBACK_PATH not in client.get("/openapi.json").json()["paths"]


def test_start_and_delete_install_over_rest(client, cards, ctx, gh):
    connection_id = _connection(cards)

    started = client.post(f"/api/tenants/default/git-connections/{connection_id}/install:start")
    assert started.status_code == 200
    assert started.json()["authorize_url"]

    state = httpx.URL(started.json()["authorize_url"]).params.get("state")
    client.get(CALLBACK_PATH, params={"state": state, "installation_id": _INSTALLATION})
    listed = client.get("/api/tenants/default/git-connections").json()
    assert listed["connections"][0]["install"]["account"] == _ACCOUNT
    assert listed["install_available"][GITHUB] is True

    removed = client.delete(f"/api/tenants/default/git-connections/{connection_id}/install")
    assert removed.status_code == 200
    assert removed.json()["removed"] is True


def test_start_install_404s_for_a_connection_this_tenant_does_not_have(client):
    assert (
        client.post("/api/tenants/default/git-connections/9999/install:start").status_code
        == _HTTP_NOT_FOUND
    )


def test_start_install_is_refused_for_another_tenant_in_the_url(client):
    assert (
        client.post("/api/tenants/acme/git-connections/1/install:start").status_code
        == _HTTP_FORBIDDEN
    )


def test_no_response_anywhere_carries_a_minted_token(client, cards, ctx, gh, settings):
    """The write-only rule of phase 3, extended to what phase 4 mints."""
    connection_id = _install_github(cards, ctx, gh)
    git_install.HTTP_TRANSPORT = gh.transport()
    cards.connection_credential(connection_id, settings).token()

    for response in (
        client.get("/api/tenants/default/git-connections"),
        client.get("/api/tenants/default/git-config"),
        client.post(f"/api/tenants/default/git-connections/{connection_id}:verify"),
    ):
        assert _MINTED not in response.text


def test_a_stored_credential_still_works_when_there_is_no_install(cards, ctx, settings):
    """Phase 3 is not replaced. Azure DevOps and self-hosted deploys live here."""
    connection_id = _connection(cards, "azure_devops", project="org/proj/repo")
    cards.set_connection_credential(connection_id, "a-pasted-credential", settings)

    credential = cards.connection_credential(connection_id, settings)

    assert credential.info.source == "tenant"
    assert credential.token() == "a-pasted-credential"


def test_the_single_configuration_shim_is_untouched_by_an_install(cards, ctx, gh, settings):
    """The phase-2 flat view keeps working — the panel and old tools read it."""
    cards.set_git_config(GitConfigUpdate(provider=GITHUB, project=_PROJECT), settings)
    connection_id = cards.connections()[0].id
    state = _start(cards, ctx, connection_id)
    git_config_ops.complete_git_install(
        cards, CallbackClaim(state, _INSTALLATION, None), transport=gh.transport()
    )

    config_payload = git_config_ops.get_git_config(cards, settings)
    assert config_payload["credential"]["source"] == "install"
    assert config_payload["credential"]["configured"] is True

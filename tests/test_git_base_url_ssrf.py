"""The stored git ``base_url`` cannot steer a credentialed request (#412).

Third instance of a defect already closed in TFactory (#1110/#1116) and PFactory
(#610/#611): a per-connection host, writable by any caller holding the ``write``
scope, that the backend then addresses WITH the tenant's credential attached.

Every test here drives a REAL READ SITE -- ``git_providers.build_provider``,
``git_config_ops.complete_git_install``, ``git_install.InstallTokenSource``.
None of them calls ``safe_git_base_url`` directly, on purpose: a suite that
tests the helper stays green when the helper stops being called, which is
exactly how an unwired guard gets certified. Delete the ``safe_git_base_url``
call from any one of those three modules and a test in this file goes red.

What the write side checked before this, and did not:
``git_config.validate_base_url`` asserts the string starts with ``http://`` or
``https://``. That refuses ``file://``. It says nothing about where the host
resolves, so ``http://169.254.169.254/latest/meta-data/`` passed it -- which is
why a scheme test is not registered as a barrier in this fleet's CodeQL packs.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import ipaddress
import logging
import socket
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cfactory import config, git_config, git_config_ops, git_install
from cfactory import cards as cards_module
from cfactory.card_ops import AuditContext
from cfactory.cards import CardStore
from cfactory.config import Settings
from cfactory.error_ref import InputRejectedError, client_error
from cfactory.git_config import GitConfigError, target_from_settings
from cfactory.git_connections import GitConnectionCreate, GitConnectionUpdate, GitRepositoryCreate
from cfactory.git_install import CallbackClaim, InstallError
from cfactory.git_providers import build_provider
from cryptography.hazmat.primitives import hashes, serialization  # noqa: F401 — see app_pem
from cryptography.hazmat.primitives.asymmetric import rsa

from cfactory.audit import AuditStore  # isort: skip

# The one address refused in BOTH postures: AWS/Azure instance credentials.
_METADATA = "http://169.254.169.254"
# A legitimate self-hosted GitLab CE on a LAN. RFC-1918, so it exists only
# because the guard runs with allow_private=True.
_PRIVATE = "http://10.11.12.13"
# A genuinely PUBLIC host. Asserted is_global below rather than assumed --
# 203.0.113.x (TEST-NET-3) reads as private to `ipaddress`, so using it would
# prove nothing beyond allow_private. A literal keeps the test off DNS.
_PUBLIC = "http://1.1.1.1"

_TOKEN = "test-not-a-credential-9f21"  # noqa: S105 — a fake, not a secret
_PROJECT = "acme/widgets"
_INSTALLATION = "4242"
_MINTED = "ghs_MINTED-INSTALLATION-TOKEN-4c1f9a"  # noqa: S105 — a fake, not a secret
_KEY = base64.b64encode(b"k1" * 16).decode()


def test_the_public_fixture_really_is_public():
    """Guards the guard: if _PUBLIC drifted to a private range the acceptance
    test below would pass for the wrong reason (allow_private) and prove nothing."""
    assert ipaddress.ip_address(_PUBLIC.removeprefix("http://")).is_global


# ── read site 1: build_provider, the RFC-0020 §3.4 credential injection point ─


def _target(base_url: str):
    return target_from_settings(
        Settings(git_provider="gitlab", git_provider_token=_TOKEN, git_provider_url=base_url)
    )


def test_build_provider_refuses_the_metadata_range():
    """The tenant credential must not be pointed at instance metadata."""
    with pytest.raises(GitConfigError, match="169.254.169.254"):
        build_provider(_target(_METADATA), _PROJECT)


def test_build_provider_accepts_a_self_hosted_host_on_a_private_range():
    """allow_private=True exists for this: a GitLab CE on a LAN is a real target."""
    provider = build_provider(_target(_PRIVATE), _PROJECT)

    assert provider is not None


def test_build_provider_accepts_an_ordinary_public_host():
    provider = build_provider(_target(_PUBLIC), _PROJECT)

    assert provider is not None


# ── read sites 2 and 3: the install callback and the token mint ──────────────


@pytest.fixture(scope="session")
def app_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def settings(app_pem) -> Settings:
    return Settings(
        credential_key=f"v1:{_KEY}",
        install_callback_base_url="https://cfactory-mcp.test",
        github_app_id="123456",
        github_app_slug="cfactory-test",
        github_app_private_key=app_pem,
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch, settings):
    for module in (cards_module, git_config, git_config_ops):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def _no_cached_tokens():
    git_install.clear_token_cache()
    yield
    git_install.clear_token_cache()
    git_install.HTTP_TRANSPORT = None


@pytest.fixture
def cards(tmp_path) -> CardStore:
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def ctx(tmp_path) -> AuditContext:
    store = AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="ssrf-test-hmac")  # noqa: S106 — a test fixture, not a secret
    return AuditContext(store, "tester")


def _github_app_transport() -> httpx.MockTransport:
    """GitHub's App endpoints. Reached only if the guard lets a request out."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v3")
        if path == f"/app/installations/{_INSTALLATION}":
            return httpx.Response(200, json={"id": int(_INSTALLATION), "account": {"login": "acme"}})
        if path == f"/app/installations/{_INSTALLATION}/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": _MINTED,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        return httpx.Response(404, json={"message": "unexpected"})

    return httpx.MockTransport(handler)


def _connection(cards: CardStore, base_url: str | None = None) -> int:
    row = cards.create_connection(GitConnectionCreate(provider="github", base_url=base_url))
    cards.create_repository(row.id, GitRepositoryCreate(project=_PROJECT, make_default=True))
    return row.id


def _state(cards: CardStore, ctx: AuditContext, connection_id: int) -> str:
    result = git_config_ops.start_git_install(cards, ctx, connection_id)
    return str(httpx.URL(str(result["authorize_url"])).params.get("state"))


def test_the_install_callback_refuses_the_metadata_range(cards, ctx):
    """The callback sends an App JWT (and, on GitLab, the deployment's client
    secret) to this host. Refused as an InstallError, so the route answers 400."""
    connection_id = _connection(cards, _METADATA)
    state = _state(cards, ctx, connection_id)

    with pytest.raises(InstallError, match="169.254.169.254"):
        git_config_ops.complete_git_install(
            cards, CallbackClaim(state, _INSTALLATION, None), transport=_github_app_transport()
        )


def test_minting_an_install_token_refuses_the_metadata_range(cards, ctx, settings):
    """A connection installed against a good host and REPOINTED afterwards must
    not mint against the new one. The panel must still list it, so the check is
    at the mint and not where the credential handle is built."""
    connection_id = _connection(cards)
    state = _state(cards, ctx, connection_id)
    git_config_ops.complete_git_install(
        cards, CallbackClaim(state, _INSTALLATION, None), transport=_github_app_transport()
    )
    cards.update_connection(connection_id, GitConnectionUpdate(base_url=_METADATA))

    credential = cards.connection_credential(connection_id, settings)
    # Listing the connection still works: a repointed connection is fixable.
    assert credential.configured
    with pytest.raises(InstallError, match="169.254.169.254"):
        credential.token()


# ── the redirect hop ─────────────────────────────────────────────────────────


def _private_tokens(headers: list[dict[str, str]]) -> list[str]:
    return [
        value
        for sent in headers
        for key, value in sent.items()
        if key.lower() == "private-token"
    ]


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Hop 2. Records the headers it is sent, so a leaked credential is visible."""

    received: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
        type(self).received.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *args) -> None:  # noqa: ARG002 — silence the test log
        return


class _Redirector(http.server.BaseHTTPRequestHandler):
    """Hop 1. Records what it got, then 302s to the other origin."""

    received: list[dict[str, str]] = []
    target = ""

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
        type(self).received.append(dict(self.headers))
        self.send_response(302)
        self.send_header("Location", type(self).target)
        self.end_headers()

    def log_message(self, *args) -> None:  # noqa: ARG002 — silence the test log
        return


def _redirector(target: str) -> type[_Redirector]:
    _Redirector.target = target
    return _Redirector


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_302_to_another_host_does_not_re_send_the_credential():
    """Two real servers, not a mock. Hop 1 answers 302; hop 2 records what it got.

    LATENT rather than live today: httpx does not follow redirects by default,
    so it raises on the 302 and hop 2 is never called. Asserted anyway because
    that default lives in the vendored, byte-gated ``providers/factory.py``
    shared by four repos -- one upstream change flips this from latent to live
    everywhere at once (Factory#825). Note httpx strips ``Authorization``
    across origins but does NOT strip GitLab's ``PRIVATE-TOKEN``, which is the
    header this provider sends and the one asserted on here.
    """
    _Recorder.received = []
    _Redirector.received = []
    hop2 = _serve(_Recorder)
    hop1 = _serve(_redirector(f"http://127.0.0.1:{hop2.server_port}/api/v4/version"))
    try:
        provider = build_provider(_target(f"http://127.0.0.1:{hop1.server_port}"), _PROJECT)
        with pytest.raises(httpx.HTTPStatusError, match="302"):
            asyncio.run(provider.api_get("/version"))

        # The request really was made and really did carry the credential --
        # without this the assertion below would hold for a provider that never
        # called anything.
        assert _private_tokens(_Redirector.received) == [_TOKEN]
        assert _private_tokens(_Recorder.received) == []
        assert _Recorder.received == [], "httpx followed the redirect; this is no longer latent"
    finally:
        hop1.shutdown()
        hop2.shutdown()


# ── the message the caller gets back (CFactory#414 / Factory#831) ─────────────
#
# `safe_git_base_url` interpolates the guard's exception into a `GitConfigError`,
# and `routes_git_config` hands that string back VERBATIM as a 400 detail (it
# rewraps `exc.args[0]` as an `InputRejectedError`, which `client_error` trusts).
# So the guard's wording is client-facing, and "an error was returned" is not
# enough to assert -- these two lock the TEXT.


def _refuse_dns(monkeypatch):
    """Make every lookup fail, deterministically, with a resolver-written string."""

    def _boom(*_args, **_kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)


def test_a_resolve_failure_does_not_leak_the_resolver_text_to_the_caller(monkeypatch):
    """CWE-209 at a real read site.

    Before the .hub-sha bump the canonical raised
    ``cannot resolve host 'x': [Errno -2] Name or service not known`` and that
    whole string reached the caller. The host is the caller's own input and
    stays; the resolver's text is third-party wording nobody here reviewed.
    """
    _refuse_dns(monkeypatch)

    with pytest.raises(GitConfigError) as caught:
        build_provider(_target("http://gitlab.internal.example.com"), _PROJECT)

    detail = client_error(
        logging.getLogger(__name__), "invalid request", InputRejectedError(caught.value.args[0])
    )
    assert "gitlab.internal.example.com" in detail
    assert "Name or service not known" not in detail
    assert "Errno" not in detail


def test_a_guard_rejection_reaches_the_caller_verbatim_not_as_a_reference_id():
    """One InputRejectedError, not two classes of the same name.

    ``client_error`` gates on ``isinstance``. The guard now raises the HUB's
    class; ``error_ref`` re-exports it rather than defining its own, so this
    holds. Define a second class here and the assert flips to a reference id --
    silently, with the response still a 400.
    """
    from factory_common.client_errors import InputRejectedError as HubInputRejectedError

    assert InputRejectedError is HubInputRejectedError

    with pytest.raises(GitConfigError) as caught:
        build_provider(_target(_METADATA), _PROJECT)

    rejection = InputRejectedError(caught.value.args[0])
    assert isinstance(rejection, InputRejectedError)
    detail = client_error(logging.getLogger(__name__), "invalid request", rejection)
    assert "169.254.169.254" in detail
    assert "reference" not in detail

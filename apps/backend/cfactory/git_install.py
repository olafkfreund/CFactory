"""The install flow that fills the phase-3 credential store (RFC-0020 §3.4, phase 4).

Phase 3 gave a connection an encrypted credential and a paste box to put one in.
This is what replaces the paste box for the two hosts that have an install
protocol, and the shape of what gets stored is the whole point:

* **GitHub — a GitHub App, never an OAuth App.** The deployment holds ONE secret,
  the App's RSA private key, supplied as configuration by whoever registered the
  App. A tenant holds an ``installation_id``, which is an identifier and not a
  secret. Every provider call mints a fresh **installation token** — scoped to
  the repositories the installer selected, expiring in about an hour, and acting
  as the App's own identity rather than impersonating the person who clicked
  install. Nothing mints into the phase-3 store: a minted token is returned to
  the caller that asked for it and cached in memory until it expires, and
  :func:`persistable_secret` is the single guard that says which of these values
  is allowed to be written down.
* **GitLab — an OAuth application with refresh.** GitLab has no App identity, so
  something long-lived does have to be kept. What is kept is the REFRESH token
  and only the refresh token, sealed by :mod:`cfactory.credentials` against
  (tenant, connection) exactly like a pasted PAT. Access tokens are obtained from
  it on demand and live in memory. GitLab rotates the refresh token on every
  exchange, so each refresh writes the new one back — which is why the token
  source is handed a writer as well as a reader.
* **Azure DevOps — out of scope, deliberately.** It keeps the phase-3 stored
  credential path. There is no half-built install flow for it here.

**The callback is the exposed surface, so state is the whole defence.** A
provider redirect arrives at a browser with no CFactory session and no
``X-Tenant-Id`` header — that is what a redirect IS — so the endpoint cannot be
put behind the auth perimeter and cannot learn the tenant from the request. It
learns it from the state:

* the state token is 256 bits from :func:`secrets.token_urlsafe`;
* only its **SHA-256** is stored, so the database never holds a usable state and
  a database read does not yield one;
* the row carries the tenant, the connection and the provider, so the tenant a
  callback lands on is the tenant that STARTED the install and never anything the
  request said;
* it is **single-use** — consumed inside the same transaction that reads it, so a
  replayed callback URL finds nothing;
* it EXPIRES (:data:`STATE_TTL_SECONDS`), so a state that leaked into a browser
  history or a proxy log stops being interesting quickly.

On top of that the provider side is checked rather than assumed. GitHub's setup
redirect carries no signature — there is none to verify — so the
``installation_id`` it names is proved by USING it: an App JWT signed with the
deployment's private key asks GitHub whether that installation exists and belongs
to this App, and a redirect naming an installation of somebody else's App is
refused. GitLab's ``code`` is verified by the exchange itself, a back-channel POST
carrying the deployment's client secret to the configured instance.

**No new dependency.** The App JWT is RS256 over two base64url segments, which
``cryptography`` (already here for AES-GCM) signs directly; a JWT library would
be a dependency for twelve lines.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .config import DEFAULT_TENANT, Settings
from .db import Base
from .git_config import GITHUB, GITLAB

logger = logging.getLogger(__name__)

# How long a pending install may sit unfinished. Ten minutes is longer than
# choosing repositories on GitHub's install screen takes and short enough that a
# state leaked into a browser history is stale before anyone reads it.
STATE_TTL_SECONDS = 600

# How long the App JWT that mints an installation token is valid. GitHub refuses
# anything over ten minutes; nine leaves room for clock skew in the other
# direction. ``iat`` is backdated for the same reason.
_APP_JWT_SECONDS = 540
_APP_JWT_BACKDATE = 60

# Mint a new installation/access token when this little of its life is left,
# rather than at the moment it expires — a token that dies mid-request is a
# board write that fails for no reason a user can act on.
_RENEW_MARGIN_SECONDS = 300

# Fallback lifetime when a provider answers without an expiry. Under GitHub's own
# hour and under GitLab's two, so the worst case is minting more often.
_DEFAULT_TOKEN_SECONDS = 1800

_TIMEOUT_SECONDS = 10.0

# The install status the panel renders. ``installed`` is the good one; the other
# is the RFC-0020 §3.4 requirement that a refresh FAILURE degrades the tenant
# rather than quietly continuing, and it is deliberately the same word
# :func:`cfactory.git_config.derive_status` already produces, so the connection
# and its install cannot disagree about whether the board can reach the host.
INSTALLED = "installed"
CREDENTIAL_MISSING = "credential_missing"

# The providers with an install flow. Azure DevOps is NOT here (RFC-0020 §3.4):
# it keeps the phase-3 stored-credential path, and offering an install button
# that cannot work would be worse than offering none.
INSTALLABLE_PROVIDERS: tuple[str, ...] = (GITHUB, GITLAB)

# ponytail: the ONE test seam for the provider calls this module makes on its own
# initiative — minting a token inside a lazily-resolved credential, which has no
# call site to thread a transport through (it happens under
# ``Credential.fetch()``, five frames below anything holding a request). Every
# call made from a ROUTE takes its transport as an argument instead. Reset it in
# a fixture; nothing in production ever sets it.
HTTP_TRANSPORT: httpx.BaseTransport | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class InstallError(RuntimeError):
    """An install that cannot be started, completed, or minted from.

    Rendered as a 400 on the callback and on ``install:start``, and RAISED rather
    than swallowed at mint time — RFC-0020 §3.4 requires a board write with no
    usable credential to fail loudly. Every consumer of a provider already treats
    an exception as "record the reason on the card and report ok=false", so
    raising is what makes the failure visible instead of turning it into an
    unauthenticated request that 401s somewhere less legible.

    Its message never contains a token, a private key or a client secret.
    """


# ── storage ──────────────────────────────────────────────────────────────────


class GitInstallRow(Base):
    """One connection's completed install. At most one per connection.

    Its own table rather than columns on the connection for the phase-3 reason:
    an install has a different lifetime from the connection it hangs off (it is
    revoked and re-done without disturbing the repositories), and it is the kind
    of thing a future ``SELECT *`` on the connection should not drag along.

    **Nothing here is a secret.** ``installation_id`` is an identifier GitHub
    prints in its own URLs; the GitHub private key is deployment configuration and
    the GitLab refresh token lives in ``tenant_git_credential``, sealed. That is
    why this row is returned to the panel verbatim and the credential row never
    is.
    """

    __tablename__ = "git_install"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT, index=True
    )
    connection_id: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32), default=GITHUB, server_default=GITHUB)
    # GitHub only: which installation of the App this tenant selected. NULL on
    # GitLab, whose equivalent state is the sealed refresh token.
    installation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The org or user the App is installed on / the GitLab account that
    # authorised, for the panel to show. Display only, never addressed.
    account: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=INSTALLED, server_default=INSTALLED)
    # Why the last mint failed, when it did. Cleared by a mint that works.
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        # One install per connection, enforced by the database rather than by a
        # check that loses the race between two concurrent callbacks.
        Index("ix_git_install_connection", "connection_id", unique=True),
    )


class InstallStateRow(Base):
    """One PENDING install: the state a callback must present to be believed.

    The row IS the state check. It carries the tenant and connection the install
    was started for, so the callback never has to be told either; it is deleted
    when consumed, so a replay finds nothing; and it stores only a hash, so
    reading the database does not yield a state anyone can present.
    """

    __tablename__ = "git_install_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # SHA-256 of the state token, hex. NOT the token: a database dump, a replica
    # or a backup must not hand anybody a usable state, and the callback only
    # needs to recognise one it minted.
    state_hash: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_TENANT)
    connection_id: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32), default=GITHUB)
    # The exact redirect_uri sent to the provider. GitLab requires the exchange to
    # repeat it byte-for-byte, and keeping the one that was SENT (rather than
    # rebuilding it later from settings) means an operator changing the callback
    # base URL mid-install fails the exchange instead of silently mismatching.
    redirect_uri: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (Index("ix_git_install_state_hash", "state_hash", unique=True),)


class GitInstall(BaseModel):
    """The wire view of one connection's install. No secret has a field here."""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    installation_id: str | None = None
    account: str | None = None
    status: str
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def install_view(row: GitInstallRow) -> GitInstall:
    return GitInstall(
        provider=row.provider,
        installation_id=row.installation_id,
        account=row.account,
        status=row.status,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ── state ────────────────────────────────────────────────────────────────────


def new_state() -> str:
    """A fresh state token. 256 bits from the CSPRNG, and never stored as-is."""
    return secrets.token_urlsafe(32)


def state_digest(state: str) -> str:
    """The stored form of a state token.

    Hashing rather than storing means the lookup is on a value that is useless to
    whoever obtains it, and it is why the lookup being a plain SQL equality is not
    a timing concern: matching the digest requires producing a preimage, not
    guessing bytes one comparison at a time.
    """
    return hashlib.sha256(state.encode()).hexdigest()


def state_expiry() -> datetime:
    return _now() + timedelta(seconds=STATE_TTL_SECONDS)


# ── deployment configuration ─────────────────────────────────────────────────


def github_private_key(settings: Settings) -> str | None:
    """The App's PEM, from the mounted file if there is one, else the env value.

    The file WINS on purpose: where a platform mounts secrets as files, the file
    is the live value and an env var left over from an earlier deploy is the stale
    one. Read at use time and not cached, so rotating the mounted key takes effect
    without a restart.
    """
    path = (settings.github_app_private_key_file or "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            # The path, never the contents. And note (#718): this specific
            # InstallError message is the reason InstallError as a TYPE is not
            # safe to mark verbatim everywhere it's caught -- error_ref.py's
            # docstring names this exact string as its motivating example of
            # what must not reach an unauthenticated caller. It IS reachable
            # from routes_install.py's OAuth callback (unauthenticated), which
            # correctly redacts via error_reference() rather than str(exc).
            # It is NOT reachable from routes_git_config.py's start_git_install
            # route (authenticated, write-scoped) -- that path only calls
            # install_available()/callback_url(), never this function -- so
            # marking InstallError safe there is a decision about which raise
            # sites are reachable from THAT call graph, not about this type in
            # general. Do not assume the other route's treatment transfers.
            raise InstallError(
                f"the GitHub App private key file {path!r} could not be read: {exc.strerror}"
            ) from None
    return settings.github_app_private_key or None


def install_available(settings: Settings) -> dict[str, bool]:
    """Which providers this DEPLOYMENT can run an install flow for.

    Rendered by the panel to decide between an install button and the paste box,
    and false for everything until an operator has registered an App — which is a
    human step this software cannot perform (see docs/guides/git-app-install.md).
    A missing callback base URL disables both: a state bound to a redirect the
    provider cannot reach is a dead end, and finding that out at the callback is
    finding out too late.
    """
    if not (settings.install_callback_base_url or "").strip():
        return {GITHUB: False, GITLAB: False}
    return {
        GITHUB: bool(
            (settings.github_app_id or "").strip()
            and (settings.github_app_slug or "").strip()
            and (settings.github_app_private_key_file or settings.github_app_private_key)
        ),
        GITLAB: bool(
            (settings.gitlab_oauth_client_id or "").strip()
            and (settings.gitlab_oauth_client_secret or "").strip()
        ),
    }


def callback_url(settings: Settings) -> str:
    """The redirect the provider is told to come back to.

    One path, and it is registered with the provider rather than chosen per
    request, because a redirect URI an attacker can influence is the classic way
    an authorization code leaves the deployment it was minted for.
    """
    base = (settings.install_callback_base_url or "").strip().rstrip("/")
    if not base:
        raise InstallError(
            "this deployment has no install callback URL configured "
            "(CFACTORY_INSTALL_CALLBACK_BASE_URL), so an install cannot be started"
        )
    return f"{base}{CALLBACK_PATH}"


# The public path the provider redirects to. Named here because the runbook, the
# route and the URL builder must agree on it exactly.
CALLBACK_PATH = "/git/install/callback"


def github_web_url(api_base_url: str) -> str:
    """GitHub's WEB host, derived from its API host.

    The install link lives on the web host and the App endpoints on the API one,
    and on GitHub Enterprise those are the same host with different paths
    (``https://ghe.example.com`` and ``https://ghe.example.com/api/v3``). Deriving
    rather than adding a second setting keeps GHES working with the ``base_url``
    the connection already has.
    """
    base = (api_base_url or "").strip().rstrip("/")
    if base in {"", "https://api.github.com"}:
        return "https://github.com"
    for suffix in ("/api/v3", "/api"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def authorize_url(provider: str, base_url: str, state: str, settings: Settings) -> str:
    """Where the browser is sent to start the install.

    GitHub gets its App's install page — the screen on which the human chooses
    WHICH REPOSITORIES the App may see, which is the property that makes an App
    better than a PAT and is not something this software can or should choose for
    them. GitLab gets a standard authorization-code redirect.
    """
    if provider == GITHUB:
        slug = (settings.github_app_slug or "").strip()
        if not slug:
            raise InstallError(
                "this deployment has no GitHub App configured "
                "(CFACTORY_GITHUB_APP_SLUG / _ID / _PRIVATE_KEY) — see "
                "docs/guides/git-app-install.md for how to register one"
            )
        query = httpx.QueryParams({"state": state})
        return f"{github_web_url(base_url)}/apps/{slug}/installations/new?{query}"
    if provider == GITLAB:
        client_id = (settings.gitlab_oauth_client_id or "").strip()
        if not client_id:
            raise InstallError(
                "this deployment has no GitLab OAuth application configured "
                "(CFACTORY_GITLAB_OAUTH_CLIENT_ID / _CLIENT_SECRET) — see "
                "docs/guides/git-app-install.md for how to register one"
            )
        query = httpx.QueryParams(
            {
                "client_id": client_id,
                "redirect_uri": callback_url(settings),
                "response_type": "code",
                "state": state,
                "scope": settings.gitlab_oauth_scope,
            }
        )
        return f"{(base_url or 'https://gitlab.com').rstrip('/')}/oauth/authorize?{query}"
    raise InstallError(
        f"{provider} has no install flow (RFC-0020 §3.4 covers "
        f"{' and '.join(INSTALLABLE_PROVIDERS)}); store a credential instead"
    )


# ── GitHub: the App JWT and the installation token ───────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt(settings: Settings, *, now: int | None = None) -> str:
    """A short-lived RS256 assertion that this process holds the App's key.

    Not a credential for any repository: it authenticates the APP to GitHub, and
    the only thing it can do is ask about, and mint tokens for, that App's own
    installations. It never leaves this process except as one Authorization header
    to the configured GitHub host, and it lives about nine minutes.
    """
    app_id = (settings.github_app_id or "").strip()
    pem = github_private_key(settings)
    if not app_id or not pem:
        raise InstallError(
            "this deployment has no GitHub App credentials configured "
            "(CFACTORY_GITHUB_APP_ID and CFACTORY_GITHUB_APP_PRIVATE_KEY[_FILE]) — see "
            "docs/guides/git-app-install.md"
        )
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        # The failure TYPE, never the key material — and ``from None`` so the
        # original exception, whose text can quote the input, is not chained into
        # whatever renders this one.
        raise InstallError(
            f"the GitHub App private key is not a usable PEM: {type(exc).__name__}"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InstallError("the GitHub App private key must be an RSA key")

    issued = int(time.time() if now is None else now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {"iat": issued - _APP_JWT_BACKDATE, "exp": issued + _APP_JWT_SECONDS, "iss": app_id},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


def _github_client(
    base_url: str, token: str, transport: httpx.BaseTransport | None
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url or "https://api.github.com",
        timeout=_TIMEOUT_SECONDS,
        transport=transport,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
        },
    )


def verify_installation(
    installation_id: str,
    base_url: str,
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Prove the installation exists AND belongs to this App. Returns its account.

    **This is the provider-side check the GitHub setup redirect does not carry.**
    The redirect is a plain browser navigation with no signature to verify, so
    believing its ``installation_id`` on sight would let anyone who reaches the
    callback with a live state point a connection at an arbitrary number. Asking
    GitHub, with an assertion only the holder of the App's private key can
    produce, is the check: an installation of somebody else's App answers 404 and
    the callback refuses it.
    """
    with _github_client(base_url, app_jwt(settings), transport) as client:
        response = client.get(f"/app/installations/{installation_id}")
        if response.status_code == httpx.codes.NOT_FOUND:
            raise InstallError(
                f"GitHub does not report installation {installation_id!r} for this App. "
                "Nothing was stored."
            )
        response.raise_for_status()
        body = response.json()
    account = body.get("account") if isinstance(body, dict) else None
    if isinstance(account, dict):
        login = account.get("login") or account.get("slug")
        return str(login) if login else None
    return None


@dataclass(frozen=True)
class CallbackClaim:
    """What a provider's redirect ASSERTS, before any of it is believed.

    One object rather than three parameters because that is exactly what it is —
    an untrusted bundle arriving together — and because keeping it together stops
    a caller passing a ``code`` where an ``installation_id`` goes. Every field is
    attacker-supplied until :func:`cfactory.git_config_ops.complete_git_install`
    has checked it.
    """

    state: str = ""
    installation_id: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class MintedToken:
    """A token that exists to be USED and then forgotten.

    Frozen and unreprable for the same reason ``HttpGitHubProvider._token`` is:
    the object is one traceback away from a log sink. It is deliberately NOT a
    thing the store can accept — see :func:`persistable_secret`.
    """

    token: str = field(repr=False)
    expires_at: datetime

    def __repr__(self) -> str:
        return f"MintedToken(expires_at={self.expires_at.isoformat()})"


def mint_installation_token(
    installation_id: str,
    base_url: str,
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> MintedToken:
    """A fresh installation token, scoped to what the installer selected.

    Lives about an hour, is not written anywhere, and is minted again when the
    cached one runs low. That is the whole argument for the App over a PAT: the
    long-lived thing this deployment holds is a signing key it can use to mint,
    not a credential to somebody's repositories.
    """
    with _github_client(base_url, app_jwt(settings), transport) as client:
        response = client.post(f"/app/installations/{installation_id}/access_tokens")
        response.raise_for_status()
        body = response.json()
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise InstallError("GitHub returned no installation token")
    return MintedToken(token, _parse_expiry(body.get("expires_at")))


def _parse_expiry(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return _now() + timedelta(seconds=_DEFAULT_TOKEN_SECONDS)


# ── GitLab: the OAuth exchange and its refresh ───────────────────────────────


@dataclass(frozen=True)
class OAuthTokens:
    """What a GitLab token exchange hands back.

    Two values with two different fates, which is the point of separating them:
    ``access`` is used and dropped, ``refresh`` is the ONLY one the store is
    allowed to keep.
    """

    access: str = field(repr=False)
    refresh: str | None = field(default=None, repr=False)
    expires_at: datetime = field(default_factory=lambda: _now() + timedelta(seconds=7200))

    def __repr__(self) -> str:
        return (
            f"OAuthTokens(refresh={'yes' if self.refresh else 'no'}, "
            f"expires_at={self.expires_at.isoformat()})"
        )


def _gitlab_token_call(
    base_url: str, form: dict[str, str], transport: httpx.BaseTransport | None
) -> OAuthTokens:
    """One POST to ``/oauth/token``, whichever grant it is.

    A back-channel call carrying the deployment's client secret to the configured
    instance — which is what makes it a verification and not a hope: a ``code``
    minted for another client, or replayed after use, does not exchange.
    """
    with httpx.Client(
        base_url=(base_url or "https://gitlab.com").rstrip("/"),
        timeout=_TIMEOUT_SECONDS,
        transport=transport,
    ) as client:
        response = client.post("/oauth/token", data=form)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # The provider's error CODE, never its echo of what we sent — a
            # GitLab error body repeats parameters, and the form above carries
            # the client secret.
            raise InstallError(
                f"GitLab refused the token request ({response.status_code}). Nothing was stored."
            )
        body = response.json()
    access = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access, str) or not access:
        raise InstallError("GitLab returned no access token")
    refresh = body.get("refresh_token")
    expires_in = body.get("expires_in")
    seconds = int(expires_in) if isinstance(expires_in, (int, float)) else _DEFAULT_TOKEN_SECONDS
    return OAuthTokens(
        access,
        refresh if isinstance(refresh, str) and refresh else None,
        _now() + timedelta(seconds=seconds),
    )


def exchange_code(
    code: str,
    redirect_uri: str,
    base_url: str,
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> OAuthTokens:
    """Turn a callback ``code`` into tokens. The GitLab half of the state check."""
    client_id = (settings.gitlab_oauth_client_id or "").strip()
    client_secret = (settings.gitlab_oauth_client_secret or "").strip()
    if not client_id or not client_secret:
        raise InstallError(
            "this deployment has no GitLab OAuth application configured "
            "(CFACTORY_GITLAB_OAUTH_CLIENT_ID / _CLIENT_SECRET)"
        )
    return _gitlab_token_call(
        base_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        transport,
    )


def refresh_tokens(
    refresh_token: str,
    base_url: str,
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> OAuthTokens:
    """A new access token from the stored refresh token.

    GitLab ROTATES the refresh token on every exchange, so the caller must write
    the new one back or the next refresh fails with a token that was already
    spent. :class:`InstallTokenSource` does exactly that.
    """
    client_id = (settings.gitlab_oauth_client_id or "").strip()
    client_secret = (settings.gitlab_oauth_client_secret or "").strip()
    if not client_id or not client_secret:
        raise InstallError(
            "this deployment has no GitLab OAuth application configured, so the stored "
            "refresh token cannot be exchanged"
        )
    return _gitlab_token_call(
        base_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        transport,
    )


# ── what may be written down, and what may not ───────────────────────────────


def persistable_secret(provider: str, tokens: OAuthTokens) -> str:
    """The ONE value an install is allowed to put in the phase-3 store.

    Every path that stores something from an install goes through here, so the
    rule has one expression rather than being re-decided at each call site:

    * **GitHub stores nothing.** Its long-lived secret is the App private key,
      which is deployment configuration, and its short-lived one is minted per
      call. An installation token in the credential store would be a secret with
      an hour of life kept for the rest of time — exactly the blast radius the App
      exists to avoid — so asking for one raises.
    * **GitLab stores the refresh token and only the refresh token.** The access
      token is returned to the caller that asked for it and never written.

    ``tokens.access`` is not returned by any branch. That is the invariant the
    mutation test pins: make this function hand back the access token and a test
    fails.
    """
    if provider == GITHUB:
        raise InstallError(
            "a GitHub installation token is never persisted (RFC-0020 §3.4): it lives about "
            "an hour and is minted on demand from the App private key"
        )
    if provider == GITLAB:
        if not tokens.refresh:
            raise InstallError(
                "GitLab returned no refresh token, so this install cannot survive the access "
                "token expiring — check the OAuth application requests an offline-capable "
                "grant. Nothing was stored."
            )
        return tokens.refresh
    raise InstallError(f"{provider} has no install flow, so it stores nothing from one")


# ── minting, with the cache and the degrade ──────────────────────────────────

# Minted tokens, keyed by (tenant, connection). In memory ONLY, and that is the
# feature: the process forgets every one of them on restart, and a replica holds
# its own rather than sharing them through a store somebody could read.
#
# ponytail: an unbounded process-local dict. One entry per connection a tenant
# actually uses, so it is bounded by the configuration in practice; if a
# deployment ever holds enough connections for that to matter, the fix is a TTL
# sweep here and not a shared cache.
_TOKEN_CACHE: dict[tuple[str, int], MintedToken] = {}


def clear_token_cache() -> None:
    """Forget every cached token. Called when an install is removed, and by tests."""
    _TOKEN_CACHE.clear()


def forget_token(tenant: str, connection_id: int) -> None:
    _TOKEN_CACHE.pop((tenant, connection_id), None)


def _fresh(minted: MintedToken | None) -> bool:
    if minted is None:
        return False
    return minted.expires_at > _now() + timedelta(seconds=_RENEW_MARGIN_SECONDS)


@dataclass(frozen=True)
class InstallTokenSource:
    """Mints the token one provider call needs, from an install.

    Constructed by :class:`cfactory.cards.CardStore`, which supplies the three
    things this module deliberately does not know how to do: read the sealed
    refresh token, write a rotated one back, and record a degradation. Keeping
    those as callbacks is what stops this module importing the store (and what
    makes every branch here testable with three lambdas).
    """

    tenant: str
    connection_id: int
    provider: str
    installation_id: str | None
    base_url: str
    settings: Settings
    # The sealed GitLab refresh token, and where a rotated one goes back.
    read_secret: Callable[[], str | None]
    write_secret: Callable[[str], None]
    # Called with the reason when minting fails: the tenant degrades to
    # ``credential_missing`` and the panel says why (RFC-0020 §3.4).
    degrade: Callable[[str], None]
    # Called when a mint SUCCEEDS after a failure, so a recovered connection stops
    # reporting an error nobody can clear.
    recover: Callable[[], None]

    def token(self) -> str:
        """The token for ONE provider call. Raises rather than returning nothing.

        Cached until it is close to expiring, so a board refresh touching twenty
        cards mints once rather than twenty times.
        """
        cached = _TOKEN_CACHE.get((self.tenant, self.connection_id))
        if _fresh(cached):
            assert cached is not None  # noqa: S101 — narrowed by _fresh
            return cached.token
        try:
            minted = self._mint()
        except Exception as exc:
            # EVERY failure degrades and re-raises. Returning None here would let
            # the provider make an unauthenticated request that fails somewhere
            # far less legible, which is the silent failure RFC-0020 §3.4 forbids.
            reason = f"{type(exc).__name__}: {exc}"[:512]
            logger.warning(
                "install token mint failed for tenant %s connection %s: %s",
                self.tenant,
                self.connection_id,
                reason,
            )
            forget_token(self.tenant, self.connection_id)
            self.degrade(reason)
            raise InstallError(
                f"could not obtain a {self.provider} token for this connection: {reason}"
            ) from exc
        _TOKEN_CACHE[(self.tenant, self.connection_id)] = minted
        self.recover()
        return minted.token

    def _mint(self) -> MintedToken:
        if self.provider == GITHUB:
            if not self.installation_id:
                raise InstallError("this install carries no installation id")
            return mint_installation_token(
                self.installation_id, self.base_url, self.settings, transport=HTTP_TRANSPORT
            )
        if self.provider == GITLAB:
            stored = self.read_secret()
            if not stored:
                raise InstallError(
                    "no refresh token is stored for this connection, so no access token can "
                    "be obtained — reconnect it in Settings > Git connections"
                )
            tokens = refresh_tokens(stored, self.base_url, self.settings, transport=HTTP_TRANSPORT)
            # GitLab rotates on every exchange: the one just used is spent, so the
            # new one is written back before the access token is handed out.
            if tokens.refresh and tokens.refresh != stored:
                self.write_secret(persistable_secret(GITLAB, tokens))
            return MintedToken(tokens.access, tokens.expires_at)
        raise InstallError(f"{self.provider} has no install flow")

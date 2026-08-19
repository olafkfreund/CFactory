"""Tenant git-config operations — ONE implementation per mutation (RFC-0019 §3.3).

The same law :mod:`cfactory.card_ops` obeys, applied to the git configuration:
every action has a REST *and* an MCP surface, and both call the functions here.
If the audit stamp or the validation lived in the route and again in the tool,
parity would be a coincidence rather than a property — and the CI gate in
``tests/test_board_parity.py`` treats a mutation without a twin as a build
failure.

Kept out of :mod:`cfactory.git_config` (which is the model + resolution layer) so
that module stays importable from ``git_providers`` without a cycle: verifying
needs a provider, and a provider needs the config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import httpx

from .card_ops import AuditContext
from .cards import CardStore
from .config import Settings, get_settings
from .git_base_url import safe_git_base_url
from .git_config import (
    CREDENTIAL_MISSING,
    GITHUB,
    GITLAB,
    UNCONFIGURED,
    GitConfigError,
    GitConfigUpdate,
    GitTarget,
    config_view,
)
from .git_connections import (
    GitConnectionCreate,
    GitConnectionRow,
    GitConnectionUpdate,
    GitRepositoryCreate,
    GitRepositoryUpdate,
    connection_view,
    repository_view,
    resolved_base_url,
)
from .git_install import (
    INSTALLABLE_PROVIDERS,
    STATE_TTL_SECONDS,
    CallbackClaim,
    InstallError,
    authorize_url,
    callback_url,
    exchange_code,
    install_available,
    persistable_secret,
    verify_installation,
)
from .git_providers import build_provider, run_sync

logger = logging.getLogger(__name__)

# HTTP statuses that mean "the host looked at your credential and said no", as
# opposed to any other reason a verify can fail. These are what turn the derived
# status into ``credential_missing`` rather than leaving a green ``configured``
# on a token the host will not accept (RFC-0020 §3.4).
#
# ponytail: status codes only. A provider that signals refusal some other way
# (GitHub answers 404 for a private repo the token cannot see) reads as a plain
# failure, which is honest — from one 404 the board genuinely cannot tell "wrong
# credential" from "wrong project", and guessing would be worse than not saying.
_REJECTED_STATUSES = frozenset({401, 403})

# Audit ``target_service`` for a git-config mutation. The same value the import
# uses — the thing being configured is the connection to the git provider.
_TARGET = "git_provider"

# Audit ``correlation_key`` for a git-config mutation. The chain's key column is
# the entity that was mutated; here that is the tenant, not a card.
_KEY_PREFIX = "tenant:"


def _record(
    ctx: AuditContext, store: CardStore, *, kind: str, ok: bool, resource: str = "git-config"
) -> None:
    """Append a git-config mutation to the shared tamper-evident audit chain.

    Every mutation here is chained exactly as a card mutation is: who changed a
    tenant's git connection, and when, is precisely the kind of change an
    operator later needs to be able to reconstruct. Credential mutations use the
    same chain and the same key, naming ``git-credential`` as the resource — one
    chain per RFC-0001a, not a second one for secrets.
    """
    ctx.audit.record(
        actor=ctx.actor,
        kind=kind,
        correlation_key=f"{_KEY_PREFIX}{store.tenant}",
        target_service=_TARGET,
        endpoint=f"{ctx.endpoint}/{store.tenant}/{resource}",
        status_code=int(HTTPStatus.OK) if ok else 0,
        ok=ok,
    )


def get_git_config(store: CardStore, settings: Settings | None = None) -> dict[str, Any]:
    """This tenant's git configuration, with its derived status.

    Never a 404: a tenant that has never configured anything has a well-defined
    answer (``status: unconfigured``), and the panel needs somewhere to render.
    """
    return config_view(store.git_target(settings or get_settings())).model_dump(mode="json")


def set_git_config(store: CardStore, ctx: AuditContext, update: GitConfigUpdate) -> dict[str, Any]:
    """Replace this tenant's git configuration, and audit it.

    A full replacement (PUT), which also clears any recorded verification — that
    verification proved a configuration this one no longer is. Raises
    :class:`~cfactory.git_config.GitConfigError` on a value the provider could
    not address; the caller renders it (400 over REST, an error payload over MCP).

    No credential is accepted or stored (RFC-0020 §3.4 owns that, phases 3-4),
    which is the copilot-settings precedent verbatim: provider and model persist,
    the key never does.
    """
    store.set_git_config(update)
    _record(ctx, store, kind="set_git_config", ok=True)
    return get_git_config(store)


def verify_git_config(
    store: CardStore,
    ctx: AuditContext,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Prove this tenant's DEFAULT repository is reachable. Never raises.

    The single-configuration shim over :func:`verify_git_connection`: it verifies
    the connection the tenant's default repository lives on, and reports the
    result in the flat phase-2 shape the Settings panel already renders.

    **Exactly one** provider call — ``get_repository_info`` — which is the
    cheapest read that answers all three questions at once: does the base URL
    resolve, is the credential accepted, and can it see the project. The outcome
    is recorded, so ``status`` becomes ``verified`` (or keeps the failure reason)
    rather than being re-derived from a hopeful guess on the next read.

    Fail-safe like every other outbound call on the board: an unreachable host is
    ``ok=False`` plus the reason, not a 500.
    """
    settings = get_settings()
    target = store.git_target(settings, actor=ctx.actor, audit=ctx.audit)
    outcome = _verify_target(store, ctx, target, transport=transport)
    if outcome.status is not None:
        return _result(
            store, ok=False, status=outcome.status, reason=outcome.reason, settings=settings
        )
    return {
        "ok": outcome.ok,
        **({"reason": outcome.reason} if outcome.reason else {}),
        **({"repository": outcome.repository} if outcome.ok else {}),
        "config": get_git_config(store, settings),
    }


@dataclass(frozen=True)
class _Outcome:
    """What one verify proved, before it is rendered in either shape.

    ``status`` is set ONLY when the verify never reached the network, which is the
    case the two surfaces report differently: nothing was recorded, because
    nothing was proved.
    """

    ok: bool
    reason: str | None = None
    repository: str | None = None
    status: str | None = None


def _verify_target(
    store: CardStore,
    ctx: AuditContext,
    target: GitTarget,
    *,
    transport: httpx.BaseTransport | None = None,
) -> _Outcome:
    """One authenticated read against *target*, recorded on its connection.

    The ONE implementation of verifying, shared by the per-connection endpoint and
    by the single-configuration shim, so the two cannot come to different
    conclusions about the same host.
    """
    if target.project is None:
        return _Outcome(False, reason="no project configured", status=UNCONFIGURED)
    # A DEGRADED INSTALL is deliberately let through (RFC-0020 §3.4 phase 4). It
    # has no usable credential either, but the useful answer for it is not "store
    # one" — it is whatever the provider says when the mint is attempted, which is
    # what a verify exists to find out. Only a connection with nothing configured
    # at all short-circuits here.
    if not (target.credential.configured or target.credential.installed):
        return _Outcome(
            False,
            reason=(
                "no credential is configured for this connection, so the project cannot be "
                "reached — store one, or connect it, in Settings > Git connections"
            ),
            status=CREDENTIAL_MISSING,
        )
    try:
        info = run_sync(
            build_provider(target, target.project, transport=transport).get_repository_info()
        )
    except Exception as exc:  # noqa: BLE001 — the never-raises contract, same as
        # github_sync.sync_card: behind the protocol sits third-party provider
        # code we do not control, and an unlisted exception type must not take
        # the panel down. Nothing is swallowed — the reason is stored on the
        # connection, returned, and audited.
        reason = f"{type(exc).__name__}: {exc}"[:512]
        logger.warning("git verify failed for tenant %s: %s", store.tenant, reason)
        _record_verification(store, target, error=reason, rejected=_is_credential_rejection(exc))
        _record(ctx, store, kind="verify_git_config", ok=False)
        return _Outcome(False, reason=reason)
    _record_verification(store, target, error=None, rejected=None)
    _record(ctx, store, kind="verify_git_config", ok=True)
    return _Outcome(
        True,
        # Only the repository's own name is echoed back — enough for a human to
        # confirm they reached what they meant to, without pouring a provider's
        # whole metadata payload through the panel.
        repository=info.get("full_name") or info.get("path_with_namespace") or info.get("name"),
    )


def _record_verification(
    store: CardStore, target: GitTarget, *, error: str | None, rejected: bool | None
) -> None:
    """Record the outcome against the connection that was verified.

    A target with no connection is one resolved from the deployment's environment
    variables; the shim's ``record_git_verification`` materialises a connection for
    it, which is the phase-2 behaviour kept verbatim.
    """
    if target.connection_id is None:
        store.record_git_verification(error=error, rejected=rejected)
        return
    store.record_connection_verification(target.connection_id, error=error, rejected=rejected)


def _is_credential_rejection(exc: Exception) -> bool:
    """Whether the host refused the CREDENTIAL, as opposed to failing otherwise."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _REJECTED_STATUSES


# ── the credential (RFC-0020 §3.4) ───────────────────────────────────────────


def set_git_credential(store: CardStore, ctx: AuditContext, secret: str) -> dict[str, Any]:
    """Store (or replace) this tenant's git credential, encrypted, and audit it.

    WRITE-ONLY: what comes back is the masked indicator — that there is one, when
    it was stored, which key wraps it — and never the credential, on this
    response or on any other. Raises
    :class:`~cfactory.credentials.CredentialError` when the deployment has no
    encryption key, which is the fail-closed rule: no key means no credential is
    stored at all, rather than one stored in the clear.

    Storing one also clears any recorded rejection, since that rejection was
    about the credential this one replaces.
    """
    info = store.set_git_credential(secret)
    _record(ctx, store, kind="set_git_credential", ok=True, resource="git-credential")
    return {"ok": True, "credential": info.model_dump(mode="json")}


def clear_git_credential(store: CardStore, ctx: AuditContext) -> dict[str, Any]:
    """Forget this tenant's git credential, and audit it.

    Idempotent: removing one that is not there is ``removed: false`` and a 200,
    not a 404 — the caller asked for a state, and that state now holds.
    """
    removed = store.clear_git_credential()
    _record(ctx, store, kind="delete_git_credential", ok=True, resource="git-credential")
    return {
        "ok": True,
        "removed": removed,
        "credential": store.git_credential().info.model_dump(mode="json"),
    }


def _result(
    store: CardStore, *, ok: bool, status: str, reason: str | None, settings: Settings | None = None
) -> dict[str, Any]:
    """A verify that never reached the network: nothing was proved, so nothing is
    recorded — the derived status already says exactly this."""
    return {
        "ok": ok,
        "reason": reason,
        "status": status,
        "config": get_git_config(store, settings),
    }


# ── connections and repositories (RFC-0020 §3.3, phase 8) ────────────────────
#
# The same law as everything above: ONE implementation per mutation, called by
# both the REST route and the MCP tool, with the audit stamp and the validation
# here rather than duplicated on each surface.


def _connection_payload(
    store: CardStore, row: GitConnectionRow, settings: Settings
) -> dict[str, Any]:
    """One connection, its repositories and its MASKED credential indicator.

    The credential is resolved for its ``info`` only — nothing here fetches a
    plaintext, so rendering the panel decrypts nothing.
    """
    return connection_view(
        row,
        store.repositories(row.id),
        store.connection_credential(row.id, settings).info,
        store.install_row(row.id),
    ).model_dump(mode="json")


def list_git_connections(store: CardStore, settings: Settings | None = None) -> dict[str, Any]:
    """Every git connection this tenant has, each with its repositories.

    Never a 404 and never an error: a tenant that has configured nothing gets an
    empty list, which is the honest answer and the one the panel can render.

    ``install_available`` is a property of the DEPLOYMENT, not of the tenant: it
    says which providers an operator has registered an App / OAuth application
    for, which is what the panel needs to choose between an install button and the
    paste box. It reports capability only — no App id, no slug, no secret.
    """
    settings = settings or get_settings()
    return {
        "connections": [_connection_payload(store, row, settings) for row in store.connections()],
        "default_repository_id": _default_repository_id(store),
        "install_available": install_available(settings),
    }


def _default_repository_id(store: CardStore) -> int | None:
    resolved = store.default_repository()
    return resolved.repository.id if resolved is not None else None


def create_git_connection(
    store: CardStore, ctx: AuditContext, body: GitConnectionCreate
) -> dict[str, Any]:
    """Add a connection to a git host, and audit it.

    No credential is accepted here — that is its own write-only endpoint, and a
    create body is exactly the kind of payload that ends up in a log.
    """
    row = store.create_connection(body)
    _record(ctx, store, kind="create_git_connection", ok=True, resource="git-connection")
    return _connection_payload(store, row, get_settings())


def update_git_connection(
    store: CardStore, ctx: AuditContext, connection_id: int, body: GitConnectionUpdate
) -> dict[str, Any]:
    """Patch a connection, and audit it. Moving it clears its verification."""
    row = store.update_connection(connection_id, body)
    _record(ctx, store, kind="update_git_connection", ok=True, resource="git-connection")
    return _connection_payload(store, row, get_settings())


def delete_git_connection(
    store: CardStore, ctx: AuditContext, connection_id: int
) -> dict[str, Any]:
    """Forget a connection, its repositories and its credential, and audit it."""
    store.delete_connection(connection_id)
    _record(ctx, store, kind="delete_git_connection", ok=True, resource="git-connection")
    return {
        "ok": True,
        "deleted": True,
        "connection_id": connection_id,
        "default_repository_id": _default_repository_id(store),
    }


def verify_git_connection(
    store: CardStore,
    ctx: AuditContext,
    connection_id: int,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Prove one connection reaches one of its repositories. Never raises.

    Verifying is per-CONNECTION because what it proves is per-connection: the host
    answers, and this credential is accepted. It proves it by reading ONE
    repository — the tenant's default when that lives on this connection,
    otherwise the connection's oldest — because a host cannot be read without
    naming something on it.

    A connection with no repositories cannot be verified and says so
    (``unconfigured``): there is nothing on it to read.
    """
    settings = get_settings()
    connection = store.connection(connection_id)
    repositories = store.repositories(connection_id)
    if not repositories:
        return {
            "ok": False,
            "status": UNCONFIGURED,
            "reason": (
                "this connection has no repositories yet, so there is nothing to verify "
                "against — add one first"
            ),
            "connection": _connection_payload(store, connection, settings),
        }
    chosen = next((repo for repo in repositories if repo.is_default), repositories[0])
    target = store.git_target_for(
        settings, repository_id=chosen.id, actor=ctx.actor, audit=ctx.audit
    )
    outcome = _verify_target(store, ctx, target, transport=transport)
    payload: dict[str, Any] = {
        "ok": outcome.ok,
        "reason": outcome.reason,
        "repository": outcome.repository,
        "verified_project": chosen.project,
        "connection": _connection_payload(store, store.connection(connection_id), settings),
    }
    if outcome.status is not None:
        payload["status"] = outcome.status
    return payload


def set_connection_credential(
    store: CardStore, ctx: AuditContext, connection_id: int, secret: str
) -> dict[str, Any]:
    """Store (or replace) ONE CONNECTION's credential, encrypted, and audit it.

    WRITE-ONLY, exactly as the per-tenant one was: what comes back is the masked
    indicator, and no surface anywhere returns the credential. Sealed against
    (tenant, connection), so the stored record is useless on any other connection.
    """
    info = store.set_connection_credential(connection_id, secret)
    _record(ctx, store, kind="set_git_credential", ok=True, resource="git-credential")
    return {
        "ok": True,
        "connection_id": connection_id,
        "credential": info.model_dump(mode="json"),
    }


def clear_connection_credential(
    store: CardStore, ctx: AuditContext, connection_id: int
) -> dict[str, Any]:
    """Forget ONE CONNECTION's credential, and audit it. Idempotent."""
    removed = store.clear_connection_credential(connection_id)
    _record(ctx, store, kind="delete_git_credential", ok=True, resource="git-credential")
    return {
        "ok": True,
        "removed": removed,
        "connection_id": connection_id,
        "credential": store.connection_credential(connection_id).info.model_dump(mode="json"),
    }


# ── the install flow (RFC-0020 §3.4, phase 4) ────────────────────────────────
#
# Two tenant-facing operations, both twinned over MCP like everything else here,
# plus the callback's completion — which is NOT twinned, and deliberately: it is
# not a board action a tenant takes, it is the provider handing a browser back.
# There is nothing for an agent to invoke, and exposing "complete an install with
# this state" as a tool would publish the one input the whole flow is defended by.


def start_git_install(store: CardStore, ctx: AuditContext, connection_id: int) -> dict[str, Any]:
    """Begin an install for one connection, and audit it.

    Hands back the provider URL to send a browser to. On GitHub that is the App's
    install page, where the human chooses WHICH REPOSITORIES the App may see —
    the choice that makes an App a smaller blast radius than a PAT, and one this
    software must not make on their behalf.

    Nothing is authenticated by this call: it mints a state, and the state is
    worthless until the provider redirects back with it. Raises
    :class:`~cfactory.git_install.InstallError` when the deployment has registered
    no App for this provider, or when the provider has no install flow at all
    (Azure DevOps — it keeps the stored-credential path).
    """
    settings = get_settings()
    connection = store.connection(connection_id)
    provider = (connection.provider or "").strip().lower()
    if provider not in INSTALLABLE_PROVIDERS:
        raise InstallError(
            f"{provider} has no install flow (RFC-0020 §3.4 covers "
            f"{' and '.join(INSTALLABLE_PROVIDERS)}) — store a credential for this connection "
            "instead"
        )
    if not install_available(settings).get(provider):
        raise InstallError(
            f"this deployment has no {provider} app registered, so an install cannot be "
            "started. An operator registers one and supplies its credentials as deployment "
            "configuration — see docs/guides/git-app-install.md"
        )
    redirect_uri = callback_url(settings)
    state = store.start_install(connection_id, redirect_uri)
    _record(ctx, store, kind="start_git_install", ok=True, resource="git-install")
    return {
        "ok": True,
        "connection_id": connection_id,
        "provider": provider,
        # The URL carries the state. It is returned exactly once, to the caller
        # that asked for it, and never stored in a form anyone else can present.
        "authorize_url": authorize_url(
            provider, resolved_base_url(provider, connection.base_url or ""), state, settings
        ),
        "redirect_uri": redirect_uri,
        "expires_in_seconds": STATE_TTL_SECONDS,
    }


def delete_git_install(store: CardStore, ctx: AuditContext, connection_id: int) -> dict[str, Any]:
    """Disconnect a connection's install, and audit it. Idempotent.

    Forgets the ``installation_id``, any sealed refresh token and any cached
    token. It does NOT uninstall at the provider — only the account owner can do
    that, on the provider's own page — so the response says so rather than
    implying a revocation that did not happen.
    """
    removed = store.delete_install(connection_id)
    _record(ctx, store, kind="delete_git_install", ok=True, resource="git-install")
    return {
        "ok": True,
        "removed": removed,
        "connection_id": connection_id,
        "note": (
            "CFactory has forgotten this install. Removing the app's access to your "
            "repositories is done on the provider's own settings page."
        ),
        "connection": _connection_payload(store, store.connection(connection_id), get_settings()),
    }


@dataclass(frozen=True)
class CompletedInstall:
    """Where a finished callback landed, for the redirect and the audit entry."""

    tenant: str
    connection_id: int
    provider: str
    account: str | None


def complete_git_install(
    store: CardStore,
    claim: CallbackClaim,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> CompletedInstall:
    """Finish an install from the provider's redirect. THE SECURITY BOUNDARY.

    *store* must be UNSCOPED: a provider redirect carries no session and no
    ``X-Tenant-Id`` (a browser navigation cannot), so the tenant is not taken from
    the request at all — it is read off the state row, and everything after that
    runs on a store scoped to THAT tenant. This is what makes it impossible to
    complete an install for a tenant other than the one that started it, whatever
    the request claims.

    In order, and every step refuses rather than guesses:

    1. the ``state`` is matched against a stored SHA-256, consumed (so a replay
       finds nothing) and checked for expiry;
    2. the tenant, connection and provider come from that row;
    3. the PROVIDER is asked to confirm what the redirect claims — GitHub, whether
       the ``installation_id`` is an installation of this App (an App JWT signed
       with the deployment's private key is the only way to ask); GitLab, by
       exchanging the ``code`` on a back-channel call carrying the client secret;
    4. only then is anything written, and what is written is an identifier
       (GitHub) or a sealed refresh token (GitLab) — never a minted token.

    Raises :class:`~cfactory.git_install.InstallError` on any of those, having
    written nothing.
    """
    settings = settings or get_settings()
    claimed = store.consume_install_state(claim.state)
    if claimed is None:
        # ONE message for forged, replayed and expired alike: telling a caller
        # which of the three it was tells them how close they got.
        raise InstallError(
            "this install link is not valid. It may have already been used, or expired — "
            "start the install again from Settings > Git connections."
        )
    scoped = store.scoped(claimed.tenant_id)
    connection = scoped.connection(claimed.connection_id)
    provider = claimed.provider
    # SSRF (#412). This host receives the deployment's GitLab client secret and a
    # GitHub App JWT below, and it came from a connection row any write-scoped
    # caller can set. Refused as an InstallError so the callback answers 400.
    try:
        base_url = safe_git_base_url(resolved_base_url(provider, connection.base_url or "")) or ""
    except GitConfigError as exc:
        raise InstallError(str(exc)) from exc

    account: str | None = None
    if provider == GITHUB:
        installation_id = (claim.installation_id or "").strip()
        if not installation_id.isdigit():
            raise InstallError("GitHub returned no installation id, so nothing was stored")
        account = verify_installation(installation_id, base_url, settings, transport=transport)
    elif provider == GITLAB:
        code = (claim.code or "").strip()
        if not code:
            raise InstallError("GitLab returned no authorization code, so nothing was stored")
        tokens = exchange_code(code, claimed.redirect_uri, base_url, settings, transport=transport)
        # persistable_secret is the ONE gate on what an install may write down: it
        # returns the refresh token and raises for anything else, so an access
        # token cannot reach the store even by mistake here.
        scoped.store_install_secret(claimed.connection_id, persistable_secret(GITLAB, tokens))
    else:  # pragma: no cover — start_git_install refuses these before a state exists
        raise InstallError(f"{provider} has no install flow")

    scoped.record_install(
        claimed.connection_id,
        provider,
        installation_id=installation_id if provider == GITHUB else None,
        account=account,
    )
    logger.info(
        "install completed for tenant %s connection %s (%s)",
        claimed.tenant_id,
        claimed.connection_id,
        provider,
    )
    return CompletedInstall(claimed.tenant_id, claimed.connection_id, provider, account)


def list_git_repositories(store: CardStore, connection_id: int | None = None) -> dict[str, Any]:
    """This tenant's repositories, or just one connection's.

    The flat list, for a caller that wants "everything I can dispatch a card to"
    without walking the connections.
    """
    return {
        "repositories": [
            repository_view(row).model_dump(mode="json")
            for row in store.repositories(connection_id)
        ],
        "default_repository_id": _default_repository_id(store),
    }


def create_git_repository(
    store: CardStore, ctx: AuditContext, connection_id: int, body: GitRepositoryCreate
) -> dict[str, Any]:
    """Add a repository to one of this tenant's connections, and audit it."""
    row = store.create_repository(connection_id, body)
    _record(ctx, store, kind="create_git_repository", ok=True, resource="git-repository")
    return repository_view(row).model_dump(mode="json")


def update_git_repository(
    store: CardStore, ctx: AuditContext, repository_id: int, body: GitRepositoryUpdate
) -> dict[str, Any]:
    """Patch a repository, and audit it."""
    row = store.update_repository(repository_id, body)
    _record(ctx, store, kind="update_git_repository", ok=True, resource="git-repository")
    return repository_view(row).model_dump(mode="json")


def delete_git_repository(
    store: CardStore, ctx: AuditContext, repository_id: int
) -> dict[str, Any]:
    """Forget a repository, and audit it.

    Cards that pointed at it are NOT deleted — they fall back to the tenant
    default, exactly like a card that never named a repository. If this was the
    default, the tenant's oldest remaining repository is promoted.
    """
    store.delete_repository(repository_id)
    _record(ctx, store, kind="delete_git_repository", ok=True, resource="git-repository")
    return {
        "ok": True,
        "deleted": True,
        "repository_id": repository_id,
        "default_repository_id": _default_repository_id(store),
    }


def set_default_git_repository(
    store: CardStore, ctx: AuditContext, repository_id: int
) -> dict[str, Any]:
    """Make one repository the tenant's default, and audit it.

    The default is what a card that names no repository resolves to — so this is
    the one setting that changes where existing, unassigned cards go.
    """
    row = store.set_default_repository(repository_id)
    _record(ctx, store, kind="set_default_git_repository", ok=True, resource="git-repository")
    return repository_view(row).model_dump(mode="json")

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
from http import HTTPStatus
from typing import Any

import httpx

from .card_ops import AuditContext
from .cards import CardStore
from .config import Settings, get_settings
from .git_config import (
    CREDENTIAL_MISSING,
    UNCONFIGURED,
    GitConfigUpdate,
    config_view,
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
    """Prove this tenant's configuration reaches its project. Never raises.

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
    if target.project is None:
        return _result(
            store, ok=False, status=UNCONFIGURED, reason="no project configured", settings=settings
        )
    if not target.credential.configured:
        return _result(
            store,
            ok=False,
            status=CREDENTIAL_MISSING,
            reason=(
                "no credential is configured for this tenant, so the project cannot be "
                "reached — store one in Settings > Git integration"
            ),
            settings=settings,
        )

    try:
        info = run_sync(
            build_provider(target, target.project, transport=transport).get_repository_info()
        )
    except Exception as exc:  # noqa: BLE001 — the never-raises contract, same as
        # github_sync.sync_card: behind the protocol sits third-party provider
        # code we do not control, and an unlisted exception type must not take
        # the panel down. Nothing is swallowed — the reason is stored on the
        # config, returned, and audited.
        reason = f"{type(exc).__name__}: {exc}"[:512]
        logger.warning("git config verify failed for tenant %s: %s", store.tenant, reason)
        store.record_git_verification(
            error=reason, rejected=_is_credential_rejection(exc), settings=settings
        )
        _record(ctx, store, kind="verify_git_config", ok=False)
        return {"ok": False, "reason": reason, "config": get_git_config(store, settings)}

    store.record_git_verification(error=None, settings=settings)
    _record(ctx, store, kind="verify_git_config", ok=True)
    return {
        "ok": True,
        # Only the repository's own name is echoed back — enough for a human to
        # confirm they reached what they meant to, without pouring a provider's
        # whole metadata payload through the panel.
        "repository": info.get("full_name") or info.get("path_with_namespace") or info.get("name"),
        "config": get_git_config(store, settings),
    }


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
    store: CardStore, *, ok: bool, status: str, reason: str, settings: Settings | None = None
) -> dict[str, Any]:
    """A verify that never reached the network: nothing was proved, so nothing is
    recorded — the derived status already says exactly this."""
    return {
        "ok": ok,
        "reason": reason,
        "status": status,
        "config": get_git_config(store, settings),
    }

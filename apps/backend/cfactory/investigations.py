"""Read-only investigations through AIFactory (#561): proposal read and card creation.

Pure board logic, no FastAPI. A refusal is an :class:`InvestigationError` carrying
the HTTP status and the machine reason; the REST layer and the MCP layer both
render it, so the two surfaces refuse identically.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx

from .card_ops import AuditContext
from .cards import Card, CardStore
from .config import DEFAULT_TENANT, Settings
from .git_connections import GitRepositoryRow, GitResourceNotFoundError
from .git_providers import EmptyRepository, HttpGitHubProvider, build_provider, run_sync

REQUEST_MAX = 2000
TITLE_PREFIX = "Investigate: "
TITLE_REQUEST_MAX = 200
_AUDIT_TARGET = "cfactory"
_HTTP_OK = 200

# A request is stored in a card and sent to AIFactory, so a pasted credential
# would be copied twice. Refused rather than redacted: a redacted request is not
# the request the human wrote.
_SECRET = re.compile(
    r"\bghp_|\bgho_|\bgithub_pat_|\bsk-|AKIA[0-9A-Z]{16}|-----BEGIN|\b(?:password|token)\s*=",
    re.IGNORECASE,
)


class InvestigationError(Exception):
    """A refusal: ``status`` is the HTTP status, ``reason`` the machine code."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _repository(store: CardStore, repository_id: int | None, *, fallback: bool) -> GitRepositoryRow:
    """The repository a request names. Only ``context`` may fall back to the default."""
    if repository_id is None:
        resolved = store.default_repository() if fallback else None
        if resolved is None:
            raise InvestigationError(404, "no_repository")
        return resolved.repository
    try:
        row = store.repository(repository_id)
        store.connection(row.connection_id)  # a repository whose connection is gone is unusable
    except GitResourceNotFoundError:
        raise InvestigationError(404, "no_repository") from None
    # Investigations are single-tenant only; rows from an earlier multi-tenant
    # period must stay out of reach.
    if row.tenant_id != DEFAULT_TENANT:
        raise InvestigationError(404, "no_repository")
    return row


def _provider(
    store: CardStore,
    settings: Settings,
    row: GitRepositoryRow,
    ctx: AuditContext,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None,
) -> HttpGitHubProvider:
    if not row.aifactory_project_id:
        raise InvestigationError(409, "no_aifactory_project")
    connection = store.connection(row.connection_id)
    if (connection.provider or "").strip().lower() != "github":
        raise InvestigationError(409, "unsupported_provider")
    target = store.git_target_for(settings, repository_id=row.id, actor=ctx.actor, audit=ctx.audit)
    if target.repository_id != row.id:  # never reach a different repository's credential
        raise InvestigationError(404, "no_repository")
    try:
        provider = build_provider(target, row.project, transport=transport)
    except ValueError:
        raise InvestigationError(502, "provider_unavailable") from None
    if not isinstance(provider, HttpGitHubProvider):  # pragma: no cover — GitHub checked above
        raise InvestigationError(409, "unsupported_provider")
    return provider


def _head(
    store: CardStore,
    settings: Settings,
    row: GitRepositoryRow,
    ctx: AuditContext,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None,
) -> tuple[str, str]:
    provider = _provider(store, settings, row, ctx, transport)
    try:
        return run_sync(provider.default_branch_head(row.project))
    except EmptyRepository:
        raise InvestigationError(409, "empty_repository") from None
    except httpx.HTTPError:
        raise InvestigationError(502, "provider_unavailable") from None


def context(
    store: CardStore,
    settings: Settings,
    repository_id: int | None,
    ctx: AuditContext,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """What an investigation of this repository would run against right now."""
    row = _repository(store, repository_id, fallback=True)
    branch, sha = _head(store, settings, row, ctx, transport)
    return {
        "repository_id": row.id,
        "repo": row.project,
        "default_branch": branch,
        "sha": sha,
        "test_command": row.investigate_test_command or None,
    }


def create_card(  # noqa: PLR0913 — the plan's signature
    store: CardStore,
    ctx: AuditContext,
    settings: Settings,
    request: str,
    repository_id: int,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
) -> Card:
    """Create an investigation card bound to an explicit repository."""
    if _SECRET.search(request):
        raise InvestigationError(422, "request_contains_secret")
    row = _repository(store, repository_id, fallback=False)
    _head(store, settings, row, ctx, transport)  # same refusals as context
    cut = len(request) > TITLE_REQUEST_MAX
    title = f"{TITLE_PREFIX}{request[:TITLE_REQUEST_MAX]}{'…' if cut else ''}"
    card = store.create_investigation_card(
        title=title,
        description=request,
        repository_id=row.id,
        binding={
            "repository_id": row.id,
            "repo": row.project,
            "aifactory_project_id": row.aifactory_project_id,
        },
    )
    ctx.audit.record(
        actor=ctx.actor,
        kind="create_investigation_card",
        correlation_key=card.card_key,
        target_service=_AUDIT_TARGET,
        endpoint=f"card/{card.card_key}:request={sha256(request)}",
        status_code=_HTTP_OK,
        ok=True,
    )
    return card

"""Read-only investigations through AIFactory (#561): proposal read and card creation.

Pure board logic, no FastAPI. A refusal is an :class:`InvestigationError` carrying
the HTTP status and the machine reason; the REST layer and the MCP layer both
render it, so the two surfaces refuse identically.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from .card_ops import AuditContext
from .cards import Card, CardRow, CardStore
from .config import DEFAULT_TENANT, Settings
from .git_connections import GitRepositoryRow, GitResourceNotFoundError
from .git_providers import EmptyRepository, HttpGitHubProvider, build_provider, run_sync

REQUEST_MAX = 2000
TITLE_PREFIX = "Investigate: "
TITLE_REQUEST_MAX = 200
_AUDIT_TARGET = "cfactory"
_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_DRAINING = 503
_HTTP_SERVER_ERROR = 500

# A request is stored in a card and sent to AIFactory, so a pasted credential
# would be copied twice. Refused rather than redacted: a redacted request is not
# the request the human wrote.
_SECRET = re.compile(
    r"\bghp_|\bgho_|\bgithub_pat_|\bsk-|AKIA[0-9A-Z]{16}|-----BEGIN|\b(?:password|token)\s*=",
    re.IGNORECASE,
)


class InvestigationError(Exception):
    """A refusal: ``status`` is the HTTP status, ``reason`` the machine code."""

    def __init__(self, status: int, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.detail = detail


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
        correlation_key=card.card_key[:128],
        target_service=_AUDIT_TARGET,
        endpoint=f"card/{card.card_key}:request={sha256(request)}"[:512],
        status_code=_HTTP_OK,
        ok=True,
    )
    return card


_FINAL = {"completed", "failed", "stopped", "expired"}
_LIMITS = {"minutes": 30, "tokens": 2_000_000, "turns": 200, "test_calls": 10}


def _record(row: Any) -> dict[str, Any]:
    value = (row.stage_runs or {}).get("investigate", {})
    return dict(value) if isinstance(value, dict) else {}


def _detail(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("detail", {})
    return dict(value) if isinstance(value, dict) else {}


def _live(record: dict[str, Any]) -> bool:
    if _detail(record).get("run_id") and _detail(record).get("pods_gone") is not True:
        return True
    status = record.get("status")
    if status == "starting" and not _detail(record).get("run_id"):
        started = record.get("started_at")
        if isinstance(started, str):
            try:
                return datetime.fromisoformat(started) > datetime.now(UTC) - timedelta(seconds=300)
            except ValueError:
                pass
        return False
    return False


def _binding_repository(
    store: CardStore, record: dict[str, Any], session: Session | None = None
) -> GitRepositoryRow:
    binding = _detail(record).get("binding")
    if not isinstance(binding, dict) or not isinstance(binding.get("repository_id"), int):
        raise InvestigationError(409, "repository_gone")
    if session is None:
        try:
            row = _repository(store, binding["repository_id"], fallback=False)
            connection = store.connection(row.connection_id)
        except (InvestigationError, GitResourceNotFoundError):
            raise InvestigationError(409, "repository_gone") from None
    else:
        locked_row, locked_connection = store.locked_investigation_repository(
            session, binding["repository_id"]
        )
        if (
            locked_row is None
            or locked_connection is None
            or locked_row.tenant_id != DEFAULT_TENANT
            or locked_connection.tenant_id != DEFAULT_TENANT
        ):
            raise InvestigationError(409, "repository_gone")
        row, connection = locked_row, locked_connection
    if (
        connection.provider.lower() != "github"
        or row.project != binding.get("repo")
        or row.aifactory_project_id != binding.get("aifactory_project_id")
    ):
        raise InvestigationError(409, "repository_changed")
    return row


def _card_for_dispatch(row: CardRow | None, request: str) -> CardRow:
    if row is None or row.kind != "investigation" or row.deleted_at is not None:
        raise InvestigationError(409, "not_investigation_card")
    if request != row.description:
        raise InvestigationError(409, "request_changed")
    record = _record(row)
    if _live(record):
        raise InvestigationError(409, "run_live")
    return row


def _aifactory(  # noqa: PLR0913 — transport, timeout and request are independent
    settings: Settings,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float | httpx.Timeout = 10,
    transport: httpx.BaseTransport | None = None,
) -> tuple[int, dict[str, Any] | None]:
    headers: dict[str, str] = {}
    if token := settings.token_for("aifactory"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(
            base_url=settings.aifactory_api_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        ) as client:
            response = client.request(method, path, json=payload)
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body if isinstance(body, dict) else None
    except httpx.RequestError:
        return 0, None


def _forgotten(status: int, body: dict[str, Any] | None) -> bool:
    """AIFactory no longer has the run, so it has no pods either. Only its exact
    "run not found": a 404 for a lost project membership must not free the card."""
    detail = (body or {}).get("detail")
    return (
        status == HTTPStatus.NOT_FOUND
        and isinstance(detail, str)
        and detail.strip().lower() == "run not found"
    )


def _relay_body(status: int, body: dict[str, Any] | None) -> dict[str, Any]:
    if status == _HTTP_OK and body is not None:
        return body
    if _HTTP_BAD_REQUEST <= status < _HTTP_SERVER_ERROR:
        raise InvestigationError(409, "upstream_refused", (body or {}).get("detail"))
    raise InvestigationError(504 if status == 0 else 502, "upstream_unavailable")


def dispatch(  # noqa: PLR0913, PLR0912, PLR0915 — the ordered ten-step protocol
    store: CardStore,
    ctx: AuditContext,
    settings: Settings,
    key: str,
    request: str,
    sha: str,
    test_command: str | None,
    limits: dict[str, Any] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str]:
    if settings.investigations != "on":
        raise InvestigationError(503, "investigations_draining")
    if settings.multi_tenant:
        raise InvestigationError(409, "multi_tenant_unsupported")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise InvestigationError(422, "invalid_sha")
    if test_command is not None and not isinstance(test_command, str):
        raise InvestigationError(422, "invalid_test_command")
    test_command = test_command or None
    if limits is not None and (
        not isinstance(limits, dict)
        or any(
            k not in _LIMITS or type(v) is not int or not 1 <= v <= _LIMITS[k]
            for k, v in limits.items()
        )
    ):
        raise InvestigationError(422, "invalid_limits")
    card = store.scoped(DEFAULT_TENANT).get(key)
    if (
        card is None
        or card.tenant_id != DEFAULT_TENANT
        or card.deleted_at is not None
        or not card.is_investigation
    ):
        raise InvestigationError(409, "not_investigation_card")
    if request != card.description:
        raise InvestigationError(409, "request_changed")
    if _live((card.stage_runs or {}).get("investigate", {})):
        raise InvestigationError(409, "run_live")
    record = (card.stage_runs or {}).get("investigate", {})
    repo = _binding_repository(store, record)
    if (repo.investigate_test_command or None) != test_command:
        raise InvestigationError(409, "test_command_changed")
    provider = _provider(store, settings, repo, ctx, transport)
    try:
        branch, _ = run_sync(provider.default_branch_head(repo.project))
        reachable = run_sync(provider.is_reachable_from_default(repo.project, branch, sha))
    except httpx.HTTPError:
        raise InvestigationError(502, "provider_unavailable") from None
    except EmptyRepository:
        raise InvestigationError(409, "sha_not_on_default_branch") from None
    if not reachable:
        raise InvestigationError(409, "sha_not_on_default_branch")
    attempt = str(uuid4())
    with store.locked_investigation(key, DEFAULT_TENANT) as (session, row):
        locked_row = _card_for_dispatch(row, request)
        record = _record(locked_row)
        repo = _binding_repository(store, record, session)
        if (repo.investigate_test_command or None) != test_command:
            raise InvestigationError(409, "test_command_changed")
        binding = _detail(record)["binding"]
        store.write_investigation(
            session,
            locked_row,
            {
                "service": "aifactory",
                "status": "starting",
                "started_at": datetime.now(UTC).isoformat(),
                "detail": {
                    "binding": binding,
                    "attempt": attempt,
                    "sha": sha,
                    "run_id": None,
                    "pods_gone": False,
                },
            },
        )
    status, body = _aifactory(
        settings,
        "POST",
        "/api/investigations",
        payload={
            "project_id": binding["aifactory_project_id"],
            "sha": sha,
            "request": request,
            "test_command": test_command,
            "limits": limits or {},
        },
        timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
        transport=transport,
    )
    run_id = body.get("id") if body else None
    if status == _HTTP_OK and isinstance(run_id, str) and run_id:
        outcome, response_status, reason = "running", 200, ""
    elif status in {400, 403, 404, 409, 422, 503}:
        outcome = "refused"
        response_status = _HTTP_DRAINING if status == _HTTP_DRAINING else 409
        reason = "investigations_draining" if status == _HTTP_DRAINING else "upstream_refused"
    else:
        outcome, response_status, reason = "uncertain", 504, "start_uncertain"
    with store.locked_investigation(key, DEFAULT_TENANT) as (session, row):
        if row is not None:
            current = _record(row)
            if _detail(current).get("attempt") == attempt:
                detail = _detail(current)
                detail["run_id"] = run_id if outcome == "running" else None
                if outcome == "refused":
                    detail["reason"] = (
                        reason if status == _HTTP_DRAINING else (body or {}).get("detail")
                    )
                current.update(status=outcome, detail=detail)
                store.write_investigation(session, row, current)
    ctx.audit.record(
        actor=ctx.actor,
        kind="investigate",
        correlation_key=key[:128],
        target_service="aifactory",
        endpoint=(
            f"card/{key}:attempt={attempt};"
            f"run={run_id if outcome == 'running' else '-'};outcome={outcome}"
        )[:512],
        status_code=response_status,
        ok=response_status == _HTTP_OK,
    )
    if outcome != "running":
        raise InvestigationError(
            response_status,
            reason,
            (body or {}).get("detail")
            if outcome == "refused" and status != _HTTP_DRAINING
            else None,
        )
    return {"run_id": str(run_id)}


def _owned(store: CardStore, key: str, run_id: str | None = None) -> dict[str, Any]:
    card = store.scoped(DEFAULT_TENANT).get(key, include_deleted=True)
    if card is None or card.tenant_id != DEFAULT_TENANT or not card.is_investigation:
        raise InvestigationError(404, "investigation_not_found")
    record = (card.stage_runs or {}).get("investigate", {})
    if run_id is not None and _detail(record).get("run_id") != run_id:
        raise InvestigationError(404, "investigation_not_found")
    result = dict(record)
    if result.get("status") == "starting" and not _live(result):
        result["status"] = "uncertain"
    return result


def _state(store: CardStore, key: str, run_id: str, body: dict[str, Any]) -> None:
    with store.locked_investigation(key, DEFAULT_TENANT) as (session, row):
        if row is None:
            return
        record = _record(row)
        detail = _detail(record)
        if detail.get("run_id") != run_id:
            return
        old_status = record.get("status")
        new_status = body.get("state")
        if isinstance(new_status, str) and (old_status not in _FINAL or new_status in _FINAL):
            record["status"] = new_status
        detail["pods_gone"] = detail.get("pods_gone") is True or body.get("pods_gone") is True
        if "reason" in body:
            detail["reason"] = body["reason"]
        record["detail"] = detail
        store.write_investigation(session, row, record)


def recover(store: CardStore, key: str) -> dict[str, Any]:
    return _owned(store, key)


def get_run(
    store: CardStore,
    settings: Settings,
    key: str,
    run_id: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    _owned(store, key, run_id)
    status, body = _aifactory(
        settings, "GET", f"/api/investigations/{quote(run_id, safe='')}", transport=transport
    )
    if _forgotten(status, body):
        _state(store, key, run_id, {"state": "gone", "pods_gone": True, "reason": "run not found"})
    result = _relay_body(status, body)
    _state(store, key, run_id, result)
    return result


def logs(  # noqa: PLR0913 — relay query and transport
    store: CardStore,
    settings: Settings,
    key: str,
    run_id: str,
    after: int = 0,
    limit: int = 100,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    _owned(store, key, run_id)
    status, body = _aifactory(
        settings,
        "GET",
        f"/api/investigations/{quote(run_id, safe='')}/logs?after={after}&limit={limit}",
        transport=transport,
    )
    return _relay_body(status, body)


def lease(  # noqa: PLR0913 — shared REST/MCP operation
    store: CardStore,
    ctx: AuditContext,
    settings: Settings,
    key: str,
    run_id: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    audit_status = 404
    try:
        _owned(store, key, run_id)
        status, body = _aifactory(
            settings,
            "POST",
            f"/api/investigations/{quote(run_id, safe='')}/lease",
            transport=transport,
        )
        audit_status = status
        if status == 0:
            audit_status = 504
        elif _HTTP_BAD_REQUEST <= status < _HTTP_SERVER_ERROR:
            audit_status = 409
        elif status != _HTTP_OK or body is None:
            audit_status = 502
        return _relay_body(status, body)
    finally:
        ctx.audit.record(
            actor=ctx.actor,
            kind="investigation_lease",
            correlation_key=key[:128],
            target_service="aifactory",
            endpoint=f"card/{key}:run={run_id}"[:512],
            status_code=audit_status,
            ok=audit_status == _HTTP_OK,
        )


def stop(  # noqa: PLR0913 — shared REST/MCP operation
    store: CardStore,
    ctx: AuditContext,
    settings: Settings,
    key: str,
    run_id: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    audit_status = 404
    try:
        _owned(store, key, run_id)
        status, body = _aifactory(
            settings,
            "POST",
            f"/api/investigations/{quote(run_id, safe='')}/stop",
            timeout=httpx.Timeout(connect=10, read=45, write=10, pool=10),
            transport=transport,
        )
        if status == 0 or status >= _HTTP_SERVER_ERROR:
            _state(store, key, run_id, {"state": "stopping"})
            audit_status = 504
            raise InvestigationError(504, "stop_uncertain")
        if _forgotten(status, body):
            _state(
                store, key, run_id, {"state": "gone", "pods_gone": True, "reason": "run not found"}
            )
        if status == _HTTP_OK and body is not None:
            _state(store, key, run_id, body)
        if status != _HTTP_OK or body is None:
            audit_status = 409 if _HTTP_BAD_REQUEST <= status < _HTTP_SERVER_ERROR else 502
            return _relay_body(status, body)
        audit_status = status
        return body
    finally:
        ctx.audit.record(
            actor=ctx.actor,
            kind="investigation_stop",
            correlation_key=key[:128],
            target_service="aifactory",
            endpoint=f"card/{key}:run={run_id}"[:512],
            status_code=audit_status,
            ok=audit_status == _HTTP_OK,
        )

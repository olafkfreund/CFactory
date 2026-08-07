"""HITL action surface: propose (advise-only), execute (audited write), plus the
audit trail and the live activity feed.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from .actions import PreparedAction, execute_action, propose
from .api_deps import ProposeRequest, action_transport_dep, audit_dep, store_dep
from .audit import AuditStore, ChainReport
from .auth import require_scope
from .config import get_settings
from .enterprise import identity_dep
from .store import WorkItemStore

router = APIRouter(tags=["actions"])


@router.post("/api/actions/propose")
async def propose_action(
    req: ProposeRequest,
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> PreparedAction:
    """Build (but do NOT execute) a PreparedAction for the given work item.

    Advise-only: this never touches an upstream service. 400 for an unknown
    kind; 404 if there's no work item for the correlation key."""
    try:
        action = await run_in_threadpool(propose, store, req.kind, req.correlation_key, req.note)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown action kind: {req.kind!r}") from None
    if action is None:
        raise HTTPException(status_code=404, detail=f"no work item for {req.correlation_key!r}")
    return action


@router.post("/api/actions/execute")
async def execute_prepared_action(
    action: PreparedAction,
    store: Annotated[WorkItemStore, Depends(store_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Run a CONFIRMED PreparedAction against its target service. This is the
    explicit write step — the caller has already reviewed the action.

    Every confirmed action is recorded in the audit log (the HITL trail)
    before the result is returned. The audit ``actor`` is the caller
    identity from the identity seam — an ``unattributed:key-<digest>``
    reference to the presented key when keys are configured, else ``"local"``.
    Never the key itself (#251).

    Requires the ``write`` scope when API keys are configured; in local
    single-user mode (no keys) it is open."""
    settings = get_settings()
    result = await run_in_threadpool(execute_action, action, settings=settings, transport=transport)
    # Remove also prunes the card from CFactory's own board, not just the
    # upstream factory. The upstream DELETE is best-effort: an orphaned/failed
    # task whose upstream is already gone (404) must STILL leave the cockpit, so
    # prune the local work item regardless of the upstream result.
    if action.kind == "delete_task":
        removed = await run_in_threadpool(store.delete, action.correlation_key)
        result["work_item_removed"] = bool(removed)
        # A 404 from the upstream means the task is ALREADY gone (e.g. an orphaned
        # duplicate keyed by a bare spec_id with no live factory task). Combined
        # with the local prune, the Remove goal — card off the board — is achieved,
        # so report success instead of surfacing a confusing "Failed (404)" for a
        # task that has, in fact, been removed.
        if removed and int(result.get("status_code", 0)) == HTTPStatus.NOT_FOUND:
            result["ok"] = True
            result["status_code"] = int(HTTPStatus.OK)
    await run_in_threadpool(
        audit.record,
        actor=actor,
        kind=action.kind,
        correlation_key=action.correlation_key,
        target_service=action.target_service,
        endpoint=action.endpoint,
        status_code=int(result.get("status_code", 0)),
        ok=bool(result.get("ok", False)),
    )
    return result


@router.get("/api/audit")
def list_audit(audit: Annotated[AuditStore, Depends(audit_dep)]) -> dict[str, object]:
    """Recent confirmed actions, newest first — the human-in-the-loop trail."""
    entries = audit.list()
    return {
        "count": len(entries),
        "entries": [e.model_dump(mode="json") for e in entries],
    }


@router.get("/api/audit/chain")
def audit_chain(audit: Annotated[AuditStore, Depends(audit_dep)]) -> ChainReport:
    """The tamper-evidence verdict for the whole trail (#309).

    Deliberately NOT folded into ``GET /api/audit``, which the Audit view polls:
    this walks every row and recomputes every HMAC, where that one reads the
    latest hundred. The store serves a cached result for 30s so the endpoint
    cannot be turned into a full-table scan per request.

    Same READ scope as the rest of ``/api/*`` (enforced by the app's key
    middleware, including on the direct-to-backend editor host). Nothing here is
    narrower than what ``GET /api/audit`` already returns to the same caller —
    that serves whole entries with their full hashes, this serves counts, ids,
    kinds and hash prefixes. The HMAC secret is never an input to the response
    and never appears in it.
    """
    return audit.report()


@router.get("/api/activity")
def list_activity(
    store: Annotated[WorkItemStore, Depends(store_dep)],
    limit: int = 50,
) -> dict[str, object]:
    """Live activity feed: completion events flattened across all work items,
    newest first. Read-only; powers the Audit page's Activity panel so it
    reflects real pipeline traffic even before any action is executed."""
    items = store.list()
    titles = {wi.correlation_key: wi.title for wi in items}
    events: list[dict[str, object]] = []
    for wi in items:
        for ev in wi.timeline:
            ts = ev.updated_at
            events.append(
                {
                    "service": ev.service.value,
                    "correlation_key": ev.correlation_key,
                    "status": ev.status,
                    "phase": ev.phase,
                    "updated_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "title": titles.get(ev.correlation_key),
                }
            )
    events.sort(key=lambda e: str(e["updated_at"]), reverse=True)
    return {"count": len(events), "activity": events[: max(0, limit)]}

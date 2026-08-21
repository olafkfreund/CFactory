"""Inbound event ingress + poll-and-hydrate refresh.

Both endpoints broadcast WorkItem updates to connected cockpits via the shared
WebSocket manager.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from . import card_ops
from .adapters import AdapterError, BaseHTTPAdapter, hydrate
from .api_deps import action_transport_dep, adapters_dep, audit_dep, cards_store_dep, store_dep
from .audit import AuditStore
from .card_intake import apply_status
from .cards import CardStore
from .error_ref import error_reference
from .models import CompletionEvent
from .store import WorkItemStore
from .ws import get_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


@router.post("/api/events")
@router.post("/api/events/completion")
async def ingest_event(
    event: CompletionEvent,
    store: Annotated[WorkItemStore, Depends(store_dep)],
    cards: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
) -> dict[str, str]:
    """Ingest an RFC-0001 completion event. Idempotent by the CloudEvents
    ``id`` (#471 cutover): a re-delivery of the same ``id`` is accepted but is
    a no-op (no timeline append, no re-broadcast), while a legitimate re-run
    carries a new ``id`` and is recorded. Both ``/api/events`` and the
    RFC-documented ``/api/events/completion`` resolve here.

    An applied event is also written back onto the planning card joined to this
    correlation, if there is one (RFC-0019 §3.2) — the board is the live view of
    the same stream, not a second copy of it. That write-back is also where an
    explicitly-driven sequence advances (RFC-0020 §3.7): the stage that just
    finished is settled and the next queued one dispatched, which is why the
    transport and the audit chain are threaded in here rather than a second
    orchestrator existing to do it."""
    work_item, applied = await run_in_threadpool(store.upsert_from_event, event)
    if applied:
        ctx = card_ops.AuditContext(audit, card_ops.SEQUENCE_ACTOR, endpoint="/api/events")
        await run_in_threadpool(
            partial(
                apply_status,
                cards,
                work_item,
                transport=transport,
                on_dispatch=card_ops.dispatch_recorder(ctx),
            )
        )
        manager = get_manager()
        await manager.broadcast({"type": "workitem", "item": work_item.model_dump(mode="json")})
    return {
        "status": "accepted" if applied else "duplicate",
        "correlation_key": event.correlation_key,
    }


@router.post("/api/refresh")
async def refresh(
    store: Annotated[WorkItemStore, Depends(store_dep)],
    adapters: Annotated[list[BaseHTTPAdapter], Depends(adapters_dep)],
) -> dict[str, object]:
    """Poll every upstream service and hydrate the store. Best-effort:
    an unreachable service is reported, not fatal."""
    result: dict[str, object] = {}
    for adapter in adapters:
        try:
            items = await run_in_threadpool(adapter.list_items)
            hydrated = await run_in_threadpool(hydrate, store, items)
            # Reconcile: drop stale non-terminal stages the upstream no longer
            # reports, so finished/removed tasks stop showing as "running".
            live_ids = {i.task_id for i in items}
            cleared = await run_in_threadpool(store.reconcile_snapshot, adapter.service, live_ids)
            result[adapter.service.value] = (
                {"hydrated": hydrated, "cleared": cleared} if cleared else hydrated
            )
        except AdapterError as exc:
            # AdapterError looks repo-owned and safe, but base.py builds it as
            # f"{service}: GET {path} failed: {exc}" around the inner httpx
            # error -- so it launders the upstream host and URL through a
            # friendly-looking type.
            ref = error_reference(
                logger, f"adapter hydrate failed for {adapter.service.value}", exc
            )
            result[adapter.service.value] = {"error": f"the provider call failed (reference {ref})"}
        finally:
            adapter.close()
    snapshot = await run_in_threadpool(store.list)
    manager = get_manager()
    await manager.broadcast(
        {"type": "snapshot", "items": [wi.model_dump(mode="json") for wi in snapshot]}
    )
    return {"refreshed": result}

"""Inbound event ingress + poll-and-hydrate refresh.

Both endpoints broadcast WorkItem updates to connected cockpits via the shared
WebSocket manager.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from .adapters import AdapterError, BaseHTTPAdapter, hydrate
from .api_deps import adapters_dep, store_dep
from .models import CompletionEvent
from .store import WorkItemStore
from .ws import get_manager

router = APIRouter(tags=["events"])


@router.post("/api/events")
@router.post("/api/events/completion")
async def ingest_event(
    event: CompletionEvent,
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> dict[str, str]:
    """Ingest an RFC-0001 completion event. Idempotent by
    (service, correlation_key, status): a duplicate is accepted but is a
    no-op (no timeline append, no re-broadcast). Both ``/api/events`` and the
    RFC-documented ``/api/events/completion`` resolve here."""
    work_item, applied = await run_in_threadpool(store.upsert_from_event, event)
    if applied:
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
            result[adapter.service.value] = {"error": str(exc)}
        finally:
            adapter.close()
    snapshot = await run_in_threadpool(store.list)
    manager = get_manager()
    await manager.broadcast(
        {"type": "snapshot", "items": [wi.model_dump(mode="json") for wi in snapshot]}
    )
    return {"refreshed": result}

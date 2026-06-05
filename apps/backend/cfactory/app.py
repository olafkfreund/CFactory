"""FastAPI application factory for the CFactory cockpit API.

Skeleton (#5) + WorkItem persistence (#6) + adapters (#7-#9) + live WebSocket
(#10): /health, an event ingress, a poll-and-hydrate refresh, a read API, and a
broadcast hub that pushes WorkItem updates to connected cockpits.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from pydantic import BaseModel

import httpx

from . import __version__
from .actions import PreparedAction, execute_action, propose
from .audit import AuditStore, get_audit_store
from .auth import require_scope
from .enterprise import identity_dep
from .adapters import AdapterError, BaseHTTPAdapter, build_adapters, hydrate
from .config import get_settings
from .copilot import Copilot, get_copilot
from .copilot.anomalies import detect_anomalies
from .copilot.tools import rollups as compute_rollups
from .copilot.tools import summarize_timeline
from .models import CompletionEvent
from .store import WorkItemStore, get_store
from .upstream_ws import start_subscribers
from .ws import get_manager


class AskRequest(BaseModel):
    question: str


class ProposeRequest(BaseModel):
    kind: str
    correlation_key: str


def store_dep() -> WorkItemStore:
    """Dependency seam — overridden in tests with a temp store."""
    return get_store()


def adapters_dep() -> list[BaseHTTPAdapter]:
    """Dependency seam — overridden in tests with mock-transport adapters."""
    return build_adapters()


def copilot_dep() -> Copilot:
    """Dependency seam — overridden in tests with a fake-runner copilot."""
    return get_copilot()


def audit_dep() -> AuditStore:
    """Dependency seam — overridden in tests with a temp AuditStore."""
    return get_audit_store()


def action_transport_dep() -> httpx.BaseTransport | None:
    """Dependency seam — overridden in tests with a MockTransport. Defaults to
    None so production uses real network transport."""
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    tasks: list[asyncio.Task[None]] = []
    if settings.subscribe_upstreams:
        tasks = start_subscribers(get_store(), get_manager(), settings)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def create_app() -> FastAPI:
    settings = get_settings()
    manager = get_manager()
    app = FastAPI(
        title="CFactory",
        version=__version__,
        summary="Agentic control-tower cockpit over the PARR pipeline.",
        lifespan=lifespan,
    )

    # Allow the Vite dev server (default :3110) to call the API during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://localhost:{settings.frontend_port}", "http://localhost:3110"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "cfactory",
            "version": __version__,
            "multi_tenant": settings.multi_tenant,
            "upstreams": {
                "aifactory": settings.aifactory_api_url,
                "pfactory": settings.pfactory_api_url,
                "tfactory": settings.tfactory_api_url,
            },
        }

    @app.post("/api/events")
    @app.post("/api/events/completion")
    async def ingest_event(
        event: CompletionEvent, store: WorkItemStore = Depends(store_dep)
    ) -> dict[str, str]:
        """Ingest an RFC-0001 completion event. Idempotent by
        (service, correlation_key, status): a duplicate is accepted but is a
        no-op (no timeline append, no re-broadcast). Both ``/api/events`` and the
        RFC-documented ``/api/events/completion`` resolve here."""
        work_item, applied = await run_in_threadpool(store.upsert_from_event, event)
        if applied:
            await manager.broadcast(
                {"type": "workitem", "item": work_item.model_dump(mode="json")}
            )
        return {
            "status": "accepted" if applied else "duplicate",
            "correlation_key": event.correlation_key,
        }

    @app.post("/api/refresh")
    async def refresh(
        store: WorkItemStore = Depends(store_dep),
        adapters: list[BaseHTTPAdapter] = Depends(adapters_dep),
    ) -> dict[str, object]:
        """Poll every upstream service and hydrate the store. Best-effort:
        an unreachable service is reported, not fatal."""
        result: dict[str, object] = {}
        for adapter in adapters:
            try:
                items = await run_in_threadpool(adapter.list_items)
                result[adapter.service.value] = await run_in_threadpool(hydrate, store, items)
            except AdapterError as exc:
                result[adapter.service.value] = {"error": str(exc)}
            finally:
                adapter.close()
        snapshot = await run_in_threadpool(store.list)
        await manager.broadcast(
            {"type": "snapshot", "items": [wi.model_dump(mode="json") for wi in snapshot]}
        )
        return {"refreshed": result}

    @app.get("/api/workitems")
    def list_workitems(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        items = store.list()
        return {"count": len(items), "items": [wi.model_dump(mode="json") for wi in items]}

    @app.get("/api/rollups")
    def get_rollups(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        return compute_rollups(store)

    @app.get("/api/anomalies")
    def get_anomalies(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        found = detect_anomalies(store)
        return {"count": len(found), "anomalies": found}

    @app.get("/api/workitems/{correlation_key}")
    def get_workitem(
        correlation_key: str, store: WorkItemStore = Depends(store_dep)
    ) -> dict[str, object]:
        wi = store.get(correlation_key)
        if wi is None:
            raise HTTPException(status_code=404, detail=f"no work item for {correlation_key!r}")
        return wi.model_dump(mode="json")

    @app.get("/api/workitems/{correlation_key}/timeline")
    def get_timeline(
        correlation_key: str, store: WorkItemStore = Depends(store_dep)
    ) -> dict[str, object]:
        summary = summarize_timeline(store, correlation_key)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"no work item for {correlation_key!r}")
        return summary

    @app.post("/api/actions/propose")
    async def propose_action(
        req: ProposeRequest, store: WorkItemStore = Depends(store_dep)
    ) -> PreparedAction:
        """Build (but do NOT execute) a PreparedAction for the given work item.

        Advise-only: this never touches an upstream service. 400 for an unknown
        kind; 404 if there's no work item for the correlation key."""
        try:
            action = await run_in_threadpool(propose, store, req.kind, req.correlation_key)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"unknown action kind: {req.kind!r}")
        if action is None:
            raise HTTPException(
                status_code=404, detail=f"no work item for {req.correlation_key!r}"
            )
        return action

    @app.post("/api/actions/execute")
    async def execute_prepared_action(
        action: PreparedAction,
        transport: httpx.BaseTransport | None = Depends(action_transport_dep),
        audit: AuditStore = Depends(audit_dep),
        _scope: str | None = Depends(require_scope("write")),
        actor: str = Depends(identity_dep),
    ) -> dict[str, object]:
        """Run a CONFIRMED PreparedAction against its target service. This is the
        explicit write step — the caller has already reviewed the action.

        Every confirmed action is recorded in the audit log (the HITL trail)
        before the result is returned. The audit ``actor`` is the caller
        identity from the identity seam (the API key when keys are configured,
        else ``"local"``).

        Requires the ``write`` scope when API keys are configured; in local
        single-user mode (no keys) it is open."""
        result = await run_in_threadpool(
            execute_action, action, settings=settings, transport=transport
        )
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

    @app.get("/api/audit")
    def list_audit(audit: AuditStore = Depends(audit_dep)) -> dict[str, object]:
        """Recent confirmed actions, newest first — the human-in-the-loop trail."""
        entries = audit.list()
        return {
            "count": len(entries),
            "entries": [e.model_dump(mode="json") for e in entries],
        }

    @app.post("/api/copilot/ask")
    async def copilot_ask(req: AskRequest, copilot: Copilot = Depends(copilot_dep)) -> dict[str, object]:
        result = await run_in_threadpool(copilot.ask, req.question)
        return {"answer": result.answer, "work_items_considered": result.work_items_considered}

    @app.websocket("/api/ws")
    async def cockpit_feed(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            while True:
                # We don't expect client messages; this keeps the socket open
                # and raises WebSocketDisconnect when the client goes away.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(websocket)

    return app


app = create_app()

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

from . import __version__
from .adapters import AdapterError, BaseHTTPAdapter, build_adapters, hydrate
from .config import get_settings
from .copilot import Copilot, get_copilot
from .models import CompletionEvent
from .store import WorkItemStore, get_store
from .upstream_ws import start_subscribers
from .ws import get_manager


class AskRequest(BaseModel):
    question: str


def store_dep() -> WorkItemStore:
    """Dependency seam — overridden in tests with a temp store."""
    return get_store()


def adapters_dep() -> list[BaseHTTPAdapter]:
    """Dependency seam — overridden in tests with mock-transport adapters."""
    return build_adapters()


def copilot_dep() -> Copilot:
    """Dependency seam — overridden in tests with a fake-runner copilot."""
    return get_copilot()


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
            "upstreams": {
                "aifactory": settings.aifactory_api_url,
                "pfactory": settings.pfactory_api_url,
                "tfactory": settings.tfactory_api_url,
            },
        }

    @app.post("/api/events")
    async def ingest_event(
        event: CompletionEvent, store: WorkItemStore = Depends(store_dep)
    ) -> dict[str, str]:
        work_item = await run_in_threadpool(store.upsert_from_event, event)
        await manager.broadcast({"type": "workitem", "item": work_item.model_dump(mode="json")})
        return {"status": "accepted", "correlation_key": event.correlation_key}

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

    @app.get("/api/workitems/{correlation_key}")
    def get_workitem(
        correlation_key: str, store: WorkItemStore = Depends(store_dep)
    ) -> dict[str, object]:
        wi = store.get(correlation_key)
        if wi is None:
            raise HTTPException(status_code=404, detail=f"no work item for {correlation_key!r}")
        return wi.model_dump(mode="json")

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

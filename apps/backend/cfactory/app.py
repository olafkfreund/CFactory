"""FastAPI application factory for the CFactory cockpit API.

Skeleton (#5) + WorkItem persistence (#6): /health, a /api/events ingress that
upserts the WorkItem correlation store, and a read API over WorkItems. Adapters
(#7-#9), WebSocket fan-in (#10) and the board (#12) build on this.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .adapters import AdapterError, BaseHTTPAdapter, build_adapters, hydrate
from .config import get_settings
from .models import CompletionEvent
from .store import WorkItemStore, get_store


def store_dep() -> WorkItemStore:
    """Dependency seam — overridden in tests with a temp store."""
    return get_store()


def adapters_dep() -> list[BaseHTTPAdapter]:
    """Dependency seam — overridden in tests with mock-transport adapters."""
    return build_adapters()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CFactory",
        version=__version__,
        summary="Agentic control-tower cockpit over the PARR pipeline.",
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
    def ingest_event(event: CompletionEvent, store: WorkItemStore = Depends(store_dep)) -> dict[str, str]:
        store.upsert_from_event(event)
        return {"status": "accepted", "correlation_key": event.correlation_key}

    @app.post("/api/refresh")
    def refresh(
        store: WorkItemStore = Depends(store_dep),
        adapters: list[BaseHTTPAdapter] = Depends(adapters_dep),
    ) -> dict[str, object]:
        """Poll every upstream service and hydrate the store. Best-effort:
        an unreachable service is reported, not fatal."""
        result: dict[str, object] = {}
        for adapter in adapters:
            try:
                result[adapter.service.value] = hydrate(store, adapter.list_items())
            except AdapterError as exc:
                result[adapter.service.value] = {"error": str(exc)}
            finally:
                adapter.close()
        return {"refreshed": result}

    @app.get("/api/workitems")
    def list_workitems(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        items = store.list()
        return {"count": len(items), "items": [wi.model_dump(mode="json") for wi in items]}

    @app.get("/api/workitems/{correlation_key}")
    def get_workitem(correlation_key: str, store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        wi = store.get(correlation_key)
        if wi is None:
            raise HTTPException(status_code=404, detail=f"no work item for {correlation_key!r}")
        return wi.model_dump(mode="json")

    return app


app = create_app()

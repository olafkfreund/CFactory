"""FastAPI application factory for the CFactory cockpit API.

Skeleton (#5): exposes /health and a stub /api/events ingress. The real
adapters (#7-#9), WebSocket fan-in (#10), and event persistence (#11) build
on this shell.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import get_settings
from .models import CompletionEvent


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
    async def ingest_event(event: CompletionEvent) -> dict[str, str]:
        # Stub: validates the normalized envelope. #11 upserts the WorkItem timeline.
        return {"status": "accepted", "correlation_key": event.correlation_key}

    return app


app = create_app()

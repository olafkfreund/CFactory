"""Liveness/readiness endpoint for k8s probes (exempt from API-key gating)."""

from __future__ import annotations

from fastapi import APIRouter

from . import __version__
from .config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": "cfactory",
        "version": __version__,
        "multi_tenant": settings.multi_tenant,
        "upstreams": {
            "aifactory": settings.aifactory_api_url,
            "pfactory": settings.pfactory_api_url,
            "tfactory": settings.tfactory_api_url,
            # OpenObserve telemetry backend — reachability-only (not a PARR
            # factory; never polled/hydrated). Listed here for Services-view parity.
            "observe": settings.observe_api_url,
        },
    }

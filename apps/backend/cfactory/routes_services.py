"""Per-upstream reachability, provider-auth pre-flight, and endpoint editing."""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from factory_common.logsafe import sanitize_log
from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from .adapters import BaseHTTPAdapter, ServiceProbe, build_adapters
from .api_deps import (
    ServiceEndpointUpdate,
    adapters_dep,
    fetch_provider_auth,
    observe_transport_dep,
    probe_observe,
    provider_health_transport_dep,
)
from .auth import require_scope
from .config import get_settings, set_service_url
from .error_ref import InputRejectedError, client_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["services"])

_ROLES = {"pfactory": "Plan", "aifactory": "Code", "tfactory": "Test"}


@router.get("/api/services")
async def services(
    adapters: Annotated[list[BaseHTTPAdapter], Depends(adapters_dep)],
    observe_transport: Annotated[httpx.BaseTransport | None, Depends(observe_transport_dep)],
) -> dict[str, object]:
    """Per-upstream reachability for the Services view. Best-effort: a probe
    failure is reported as offline, never fatal."""
    settings = get_settings()
    urls = {
        "aifactory": settings.aifactory_api_url,
        "pfactory": settings.pfactory_api_url,
        "tfactory": settings.tfactory_api_url,
    }
    out: list[dict[str, object]] = []
    for adapter in adapters:
        name = adapter.service.value
        try:
            probe = await run_in_threadpool(adapter.probe)
        except Exception:  # never fatal — logged and reported as an offline probe
            logger.exception("[cfactory] probe failed service=%s", name)
            probe = ServiceProbe(online=False, status="error", detail="probe failed")
        finally:
            adapter.close()
        out.append(
            {
                "name": name,
                "role": _ROLES.get(name, "—"),
                "url": urls.get(name, ""),
                "online": probe.online,
                "status": probe.status,
                "detail": probe.detail,
            }
        )
    # OpenObserve ("observe") — reachability-only. NOT a PARR factory: it has no
    # list/completion API and never becomes an adapter, so it can't be probed via
    # the adapter loop above. Probe its REAL OpenObserve health path (GET /healthz
    # → 200 "ok") directly, in-cluster (no SSO). Best-effort: never fatal.
    observe_probe = await run_in_threadpool(
        lambda: probe_observe(settings.observe_api_url, transport=observe_transport)
    )
    out.append(
        {
            "name": "observe",
            "role": "Observe",
            "url": settings.observe_api_url,
            "online": observe_probe.online,
            "status": observe_probe.status,
            "detail": observe_probe.detail,
        }
    )
    return {"services": out}


@router.get("/api/provider-health")
async def provider_health(
    transport: Annotated[httpx.BaseTransport | None, Depends(provider_health_transport_dep)],
) -> dict[str, object]:
    """Per-service provider-auth pre-flight tile (RFC-0008 §3.4, #109).

    Reads each factory's ``/api/health.provider_auth`` (config pre-flight:
    which provider credentials are present) and flags a service that is
    reachable, reports the block, and has NO usable provider credential — the
    "can't authenticate to any model" alert (the expired/un-rotated-token
    class from the taskboard demo). Best-effort: never fatal."""
    settings = get_settings()
    urls = {
        "pfactory": settings.pfactory_api_url,
        "aifactory": settings.aifactory_api_url,
        "tfactory": settings.tfactory_api_url,
    }
    out: list[dict[str, object]] = []
    for name, url in urls.items():
        info = await run_in_threadpool(lambda u=url: fetch_provider_auth(u, transport=transport))
        out.append({"name": name, "role": _ROLES[name], "url": url, **info})
    alert_count = sum(
        1 for s in out if s["reachable"] and s["reported"] and not s["any_configured"]
    )
    return {"services": out, "alert_count": alert_count}


@router.put("/api/services/{name}")
async def update_service(
    name: str,
    update: ServiceEndpointUpdate,
    _scope: Annotated[str | None, Depends(require_scope("write"))],
) -> dict[str, object]:
    """Edit an upstream service's endpoint URL. Persisted to the workspace so
    it survives a restart, and effective immediately (adapters/health/refresh
    read the live setting). Requires the ``write`` scope when API keys are set."""
    settings = get_settings()
    try:
        await run_in_threadpool(set_service_url, name, update.url)
    except InputRejectedError as exc:
        # set_service_url raises InputRejectedError directly (#718) -- marked
        # at the source, since only the raise site knows its message is
        # developer-written about the caller's own input.
        raise HTTPException(
            status_code=400, detail=client_error(logger, "invalid service update", exc)
        ) from exc
    probe = ServiceProbe(online=False, status="error", detail=None)
    for adapter in build_adapters():  # fresh — reads the just-updated setting
        if adapter.service.value == name:
            try:
                probe = await run_in_threadpool(adapter.probe)
            except Exception:  # never fatal — logged and reported as an offline probe
                logger.exception("[cfactory] probe failed service=%s", sanitize_log(name))
                probe = ServiceProbe(online=False, status="error", detail="probe failed")
        adapter.close()
    return {
        "name": name,
        "role": _ROLES.get(name, "—"),
        "url": getattr(settings, f"{name}_api_url"),
        "online": probe.online,
        "status": probe.status,
        "detail": probe.detail,
    }

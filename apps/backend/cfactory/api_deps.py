"""Shared dependency seams, request models, and probe helpers for the cockpit
routers.

Every ``*_dep`` here is an injectable seam that tests override via
``app.dependency_overrides[<dep>]``. They are imported by the route modules and
re-exported from :mod:`cfactory.app` so existing test imports
(``from cfactory.app import store_dep``) keep working unchanged. Moving them out
of ``app.create_app`` is a pure relocation — the function objects are the same,
so the override keys are identical.
"""

from __future__ import annotations

import httpx
from fastapi import Header, HTTPException, WebSocket
from pydantic import BaseModel

from .adapters import BaseHTTPAdapter, ServiceProbe, build_adapters
from .audit import AuditStore, get_audit_store
from .auth import READ, authorize_headers, get_keystore
from .config import get_settings, resolve_tenant
from .copilot import Copilot, get_copilot
from .live_agent_proxy import ConnectFn
from .progress import LiveProgressHub, get_progress_hub
from .store import WorkItemStore, get_store


class AskRequest(BaseModel):
    question: str


class ProposeRequest(BaseModel):
    kind: str
    correlation_key: str
    note: str | None = None  # free-text reason, e.g. for reject_review


class ServiceEndpointUpdate(BaseModel):
    url: str


class CopilotSettingsUpdate(BaseModel):
    provider: str
    model: str


def store_dep(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> WorkItemStore:
    """Dependency seam — overridden in tests with a temp store.

    Tenant scoping (#172): when CFACTORY_MULTI_TENANT is on, every route gets a
    view of the store scoped to the tenant resolved from ``X-Tenant-Id`` (the
    header oauth2-proxy injects from the Keycloak tenant claim) — reads filter
    by it, writes stamp it. Off (local default): the unscoped store, exactly
    the pre-#172 behaviour.
    """
    settings = get_settings()
    if settings.multi_tenant:
        return get_store().scoped(resolve_tenant(x_tenant_id, settings))
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


def progress_hub_dep() -> LiveProgressHub:
    """Dependency seam — overridden in tests with a fresh hub."""
    return get_progress_hub()


def action_transport_dep() -> httpx.BaseTransport | None:
    """Dependency seam — overridden in tests with a MockTransport. Defaults to
    None so production uses real network transport."""
    return None


# OpenObserve's real health endpoint: GET /healthz returns 200 "ok" when up.
# Kept as a per-service map (not a single fixed path) so the probe is health-path
# aware — the PARR factories use their own list/health paths via their adapters,
# while observe uses OpenObserve's /healthz.
OBSERVE_HEALTH_PATH = "/healthz"

# HTTP 200 — extracted so the observe probe comparison is not a magic literal.
_HTTP_OK = 200


def observe_transport_dep() -> httpx.BaseTransport | None:
    """Dependency seam for the observe reachability probe — overridden in tests
    with a MockTransport. Defaults to None so production uses real transport."""
    return None


def provider_health_transport_dep() -> httpx.BaseTransport | None:
    """Dependency seam for the per-service provider-auth health fetch (#109) —
    overridden in tests with a MockTransport. None → real transport."""
    return None


def fleet_transport_dep() -> httpx.AsyncBaseTransport | None:
    """Dependency seam for the fleet agent-skills aggregate's sibling fetches
    (RFC-0019 §3.4) — overridden in tests with a MockTransport. None → real
    transport. Async, unlike the probes above: the three siblings are fetched
    concurrently so one slow origin cannot serialise the others."""
    return None


def live_agent_connect_dep() -> ConnectFn:
    """Dependency seam for the upstream rmux WS client — overridden in tests with
    a fake upstream. Defaults to the real ``websockets.connect``."""
    import websockets  # noqa: PLC0415 — lazy: keep the websockets import off the hot path

    return websockets.connect


def fetch_provider_auth(
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 3.0,
) -> dict[str, object]:
    """Fetch a service's provider-credential pre-flight from GET /api/health (#109).

    Returns ``{reachable, reported, any_configured, providers, error}``. A service
    that doesn't yet emit the ``provider_auth`` block (older build) is reachable
    but ``reported=False`` — surfaced as "unknown", never a false alert.
    """
    try:
        with httpx.Client(base_url=base_url, timeout=timeout, transport=transport) as c:
            resp = c.get("/api/health")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        return {
            "reachable": False,
            "reported": False,
            "any_configured": False,
            "providers": [],
            "error": str(exc),
        }
    pa = data.get("provider_auth") if isinstance(data, dict) else None
    if not isinstance(pa, dict):
        return {
            "reachable": True,
            "reported": False,
            "any_configured": False,
            "providers": [],
            "error": None,
        }
    return {
        "reachable": True,
        "reported": True,
        "any_configured": bool(pa.get("any_configured", False)),
        "providers": pa.get("providers", []),
        "error": None,
    }


def probe_observe(
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 4.0,
) -> ServiceProbe:
    """Reachability probe for the OpenObserve telemetry backend, hitting its REAL
    health path (GET /healthz → 200 "ok"). In-cluster (direct service URL), so no
    SSO/auth is involved. Mirrors ServiceProbe's classification: 200 → online, a
    connect/timeout/transport error → offline, any other HTTP status → error.
    Best-effort — never raises."""
    try:
        with httpx.Client(base_url=base_url, timeout=timeout, transport=transport) as client:
            resp = client.get(OBSERVE_HEALTH_PATH)
    except httpx.HTTPError as exc:
        return ServiceProbe(online=False, status="offline", detail=str(exc))
    code = resp.status_code
    if code == _HTTP_OK:
        return ServiceProbe(online=True, status="online")
    return ServiceProbe(online=False, status="error", detail=f"{code} {resp.reason_phrase}")


async def reject_unauthorized_ws(websocket: WebSocket) -> bool:
    """Close the WS during the handshake (1008) if the keystore is configured
    and the request carries no valid READ key. The cockpit reaches WS through
    nginx (which injects the key); the editor sends its own. Returns True when
    the socket was rejected so the caller can return early."""
    try:
        authorize_headers(
            get_keystore(),
            websocket.headers.get("authorization"),
            websocket.headers.get("x-api-key"),
            READ,
        )
        return False
    except HTTPException:
        await websocket.close(code=1008)
        return True

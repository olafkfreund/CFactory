"""FastAPI application factory for the CFactory cockpit API.

Skeleton (#5) + WorkItem persistence (#6) + adapters (#7-#9) + live WebSocket
(#10): /health, an event ingress, a poll-and-hydrate refresh, a read API, and a
broadcast hub that pushes WorkItem updates to connected cockpits.

The HTTP surface is split into cohesive ``APIRouter`` modules (``routes_*``);
this factory wires them together, installs the API-key middleware, and owns the
background-task lifespan. The per-request dependency seams and request models
live in :mod:`cfactory.api_deps` and are re-exported here so existing imports
(``from cfactory.app import store_dep``) keep working.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import (
    __version__,
    routes_actions,
    routes_cards,
    routes_connect,
    routes_copilot,
    routes_events,
    routes_git_config,
    routes_health,
    routes_install,
    routes_live_agents,
    routes_services,
    routes_well_known,
    routes_workitems,
    routes_ws,
)

# Dependency seams + request models, re-exported for test compatibility: tests do
# ``from cfactory.app import store_dep`` and register ``dependency_overrides`` with
# these exact function objects. The route modules import the same objects from
# ``cfactory.api_deps``, so the override keys line up.
from .api_deps import (  # noqa: F401 — public re-export surface
    OBSERVE_HEALTH_PATH,
    AskRequest,
    CopilotSettingsUpdate,
    ProposeRequest,
    ServiceEndpointUpdate,
    action_transport_dep,
    adapters_dep,
    audit_dep,
    cards_store_dep,
    copilot_dep,
    fleet_transport_dep,
    live_agent_connect_dep,
    observe_transport_dep,
    progress_hub_dep,
    provider_health_transport_dep,
    store_dep,
)
from .auth import READ, authorize_headers, get_keystore
from .cards import get_cards_store
from .config import (
    check_audit_secret,
    get_settings,
    load_copilot_overrides,
    load_service_overrides,
)
from .issue_import import poll_forever
from .progress import get_progress_hub, start_progress, stop_progress
from .store import get_store
from .upstream_ws import start_subscribers
from .ws import get_manager

# Endpoints exempt from API-key enforcement: the idempotent inbound event webhook
# is machine-to-machine. (/health, /mcp, and the API docs are not under the
# guarded /api//connect prefixes in the first place.)
_EXEMPT_PREFIXES = ("/api/events",)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    # Refuse to silently boot a hosted deploy on the forgeable dev audit secret (#81).
    check_audit_secret(settings)
    # Adopt every pre-phase-8 single git configuration into a connection + a
    # default repository, re-sealing its credential onto the connection without
    # ever writing the plaintext (RFC-0020 §3.3 phase 8). Idempotent and ordered
    # BEFORE the seed: a tenant that has a legacy row must be adopted, not
    # re-seeded from environment variables it has since edited away from.
    cards = get_cards_store(settings)
    cards.adopt_legacy_git_config(settings)
    # One-release seed of the default tenant's git config from the legacy env
    # vars (RFC-0020 §3.3), so an existing single-tenant deployment keeps working
    # with no operator action and its values become editable in the portal. A
    # no-op once a connection exists — see CardStore.seed_git_config_from_env.
    cards.seed_git_config_from_env(settings)
    tasks: list[asyncio.Task[None]] = []
    if settings.subscribe_upstreams:
        tasks = start_subscribers(get_store(), get_manager(), settings)
    if settings.import_poll:
        # Poll-based issue reconciliation (RFC-0020 §3.6). NOT live — see
        # cfactory.issue_import. Off unless CFACTORY_IMPORT_POLL is set.
        tasks.append(asyncio.create_task(poll_forever(get_cards_store(settings), settings)))
    progress_tasks = start_progress(get_progress_hub(), get_manager(), settings)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await stop_progress(progress_tasks)


def create_app() -> FastAPI:
    settings = get_settings()
    load_service_overrides(settings)  # apply any persisted endpoint edits
    load_copilot_overrides(settings)  # apply any persisted copilot provider/model
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

    # ── API key enforcement (#73 follow-up: token-gated API) ──────────────────
    # When the keystore is configured (CFACTORY_API_KEYS set), every request to
    # the data/action surface (/api/* and /connect/*) must carry a key with the
    # READ scope; write endpoints additionally require WRITE via require_scope.
    # In OPEN mode (no keys) this is a no-op, so local/dev and the current prod
    # posture are unchanged. The browser cockpit reaches this through nginx, which
    # injects the key; programmatic clients (the editor) send their own bearer.
    #
    # Exemptions: /health (k8s probes), the read-only MCP server (/mcp — its own
    # CFACTORY_MCP_SECRET bearer), the idempotent inbound event webhook
    # (/api/events*, machine-to-machine), and the API docs.
    @app.middleware("http")
    async def enforce_api_key(request: Request, call_next):
        path = request.url.path
        guarded = path.startswith("/api/") or path.startswith("/connect/")
        if guarded and not path.startswith(_EXEMPT_PREFIXES):
            keystore = get_keystore()
            try:
                authorize_headers(
                    keystore,
                    request.headers.get("authorization"),
                    request.headers.get("x-api-key"),
                    READ,
                )
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
        return await call_next(request)

    app.include_router(routes_health.router)
    # Public capability manifest (RFC-0019 §3.4) — unauthenticated by design; it
    # sits outside the guarded /api//connect prefixes, like /health and /mcp.
    app.include_router(routes_well_known.router)
    app.include_router(routes_events.router)
    app.include_router(routes_services.router)
    app.include_router(routes_workitems.router)
    app.include_router(routes_live_agents.router)
    app.include_router(routes_actions.router)
    app.include_router(routes_cards.router)
    app.include_router(routes_git_config.router)
    # The git-provider install callback (RFC-0020 §3.4 phase 4). Outside the
    # guarded /api//connect prefixes on purpose and by necessity: a provider
    # redirect is a browser navigation with no API key and no session, so it
    # cannot present one. It is served on the MCP host, which already bypasses
    # oauth2-proxy — no exemption is added to the perimeter that fronts the
    # cockpit. Its own defence is the single-use, hashed, tenant-bound state plus
    # a provider round trip; see routes_install's module docstring.
    app.include_router(routes_install.router)
    app.include_router(routes_copilot.router)
    app.include_router(routes_connect.router)
    app.include_router(routes_ws.router)

    # Read-only MCP server at POST /mcp — the single PARR-pipeline visibility
    # surface for external agents (Claude Code, the /parr-run conductor).
    from .mcp import router as mcp_router

    app.include_router(mcp_router)

    # Factory#516 — OTLP distributed tracing. Last, so the FastAPI
    # instrumentation sees the full route table. A no-op unless
    # OTEL_EXPORTER_OTLP_ENDPOINT is set, which it is only in-cluster, so
    # tests and local runs pay nothing. When it IS set, init_tracing()
    # proves the endpoint and credential with one empty export before it
    # claims to be enabled — see tracing.py. Deferred like the mcp router
    # above: the module pulls the OTel SDK, which must not be an import-time
    # cost of `import cfactory.app`.
    from .tracing import init_tracing  # noqa: PLC0415

    init_tracing(app)

    return app


app = create_app()

"""FastAPI application factory for the CFactory cockpit API.

Skeleton (#5) + WorkItem persistence (#6) + adapters (#7-#9) + live WebSocket
(#10): /health, an event ingress, a poll-and-hydrate refresh, a read API, and a
broadcast hub that pushes WorkItem updates to connected cockpits.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from pydantic import BaseModel

import httpx

from . import __version__
from .actions import PreparedAction, execute_action, propose
from .audit import AuditStore, get_audit_store
from .auth import (
    READ,
    KeyStore,
    authorize_headers,
    get_keystore,
    keystore_dep,
    require_scope,
)
from .connect import build_redirect, validate_redirect
from .enterprise import identity_dep
from .adapters import (
    AdapterError,
    AIFactoryAdapter,
    BaseHTTPAdapter,
    ServiceProbe,
    build_adapters,
    hydrate,
)
from .config import (
    COPILOT_PROVIDERS,
    check_audit_secret,
    get_settings,
    load_copilot_overrides,
    load_service_overrides,
    set_copilot_settings,
    set_service_url,
)
from .copilot import Copilot, get_copilot, provider_status, reset_copilot
from .copilot.anomalies import detect_anomalies
from .copilot.tools import rollups as compute_rollups
from .copilot.tools import summarize_timeline, token_by_worker, token_totals
from .live_agents import LiveAgentsResult, discover_live_agents
from .live_agent_proxy import ConnectFn, proxy_agent_console
from .task_process import build_process_detail
from .models import CompletionEvent, Service
from .progress import LiveProgressHub, get_progress_hub, start_progress, stop_progress
from .store import WorkItemStore, get_store
from .upstream_ws import start_subscribers
from .ws import get_manager


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


def progress_hub_dep() -> LiveProgressHub:
    """Dependency seam — overridden in tests with a fresh hub."""
    return get_progress_hub()


def action_transport_dep() -> httpx.BaseTransport | None:
    """Dependency seam — overridden in tests with a MockTransport. Defaults to
    None so production uses real network transport."""
    return None


def live_agent_connect_dep() -> ConnectFn:
    """Dependency seam for the upstream rmux WS client — overridden in tests with
    a fake upstream. Defaults to the real ``websockets.connect``."""
    import websockets

    return websockets.connect


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Refuse to silently boot a hosted deploy on the forgeable dev audit secret (#81).
    check_audit_secret(settings)
    tasks: list[asyncio.Task[None]] = []
    if settings.subscribe_upstreams:
        tasks = start_subscribers(get_store(), get_manager(), settings)
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
    _EXEMPT_PREFIXES = ("/api/events",)

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
                hydrated = await run_in_threadpool(hydrate, store, items)
                # Reconcile: drop stale non-terminal stages the upstream no longer
                # reports, so finished/removed tasks stop showing as "running".
                live_ids = {i.task_id for i in items}
                cleared = await run_in_threadpool(
                    store.reconcile_snapshot, adapter.service, live_ids
                )
                result[adapter.service.value] = (
                    {"hydrated": hydrated, "cleared": cleared} if cleared else hydrated
                )
            except AdapterError as exc:
                result[adapter.service.value] = {"error": str(exc)}
            finally:
                adapter.close()
        snapshot = await run_in_threadpool(store.list)
        await manager.broadcast(
            {"type": "snapshot", "items": [wi.model_dump(mode="json") for wi in snapshot]}
        )
        return {"refreshed": result}

    @app.get("/api/services")
    async def services(
        adapters: list[BaseHTTPAdapter] = Depends(adapters_dep),
    ) -> dict[str, object]:
        """Per-upstream reachability for the Services view. Best-effort: a probe
        failure is reported as offline, never fatal."""
        roles = {"pfactory": "Plan", "aifactory": "Code", "tfactory": "Test"}
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
            except Exception as exc:  # noqa: BLE001 — never fatal
                probe = ServiceProbe(online=False, status="error", detail=str(exc))
            finally:
                adapter.close()
            out.append(
                {
                    "name": name,
                    "role": roles.get(name, "—"),
                    "url": urls.get(name, ""),
                    "online": probe.online,
                    "status": probe.status,
                    "detail": probe.detail,
                }
            )
        return {"services": out}

    @app.put("/api/services/{name}")
    async def update_service(
        name: str,
        update: ServiceEndpointUpdate,
        _scope: str | None = Depends(require_scope("write")),
    ) -> dict[str, object]:
        """Edit an upstream service's endpoint URL. Persisted to the workspace so
        it survives a restart, and effective immediately (adapters/health/refresh
        read the live setting). Requires the ``write`` scope when API keys are set."""
        try:
            await run_in_threadpool(set_service_url, name, update.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        roles = {"pfactory": "Plan", "aifactory": "Code", "tfactory": "Test"}
        probe = ServiceProbe(online=False, status="error", detail=None)
        for adapter in build_adapters():  # fresh — reads the just-updated setting
            if adapter.service.value == name:
                try:
                    probe = await run_in_threadpool(adapter.probe)
                except Exception as exc:  # noqa: BLE001 — never fatal
                    probe = ServiceProbe(online=False, status="error", detail=str(exc))
            adapter.close()
        return {
            "name": name,
            "role": roles.get(name, "—"),
            "url": getattr(settings, f"{name}_api_url"),
            "online": probe.online,
            "status": probe.status,
            "detail": probe.detail,
        }

    @app.get("/api/workitems")
    def list_workitems(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        items = store.list()
        return {"count": len(items), "items": [wi.model_dump(mode="json") for wi in items]}

    @app.get("/api/rollups")
    def get_rollups(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        return compute_rollups(store)

    @app.get("/api/tokens")
    def get_tokens(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        return token_totals(store)

    @app.get("/api/tokens/by_worker")
    def get_tokens_by_worker(
        store: WorkItemStore = Depends(store_dep),
    ) -> dict[str, object]:
        """Per-worker / per-provider / per-model breakdown (RFC-0001 v1.3).

        Additive to ``/api/tokens`` (which keeps its by_service/by_work_item
        shape): exposes the drill-down ingested from live ``phase:"worker"``
        sub-events + terminal breakdowns. Empty when nothing is instrumented."""
        return token_by_worker(store)

    @app.get("/api/progress")
    def get_progress(hub: LiveProgressHub = Depends(progress_hub_dep)) -> dict[str, object]:
        items = hub.snapshot()
        return {"count": len(items), "items": [p.model_dump(mode="json") for p in items]}

    @app.get("/api/anomalies")
    def get_anomalies(store: WorkItemStore = Depends(store_dep)) -> dict[str, object]:
        found = detect_anomalies(store)
        return {"count": len(found), "anomalies": found}

    @app.get("/api/live-agents")
    async def get_live_agents(
        adapters: list[BaseHTTPAdapter] = Depends(adapters_dep),
    ) -> dict[str, object]:
        """Currently-active AIFactory agent sessions the cockpit can stream (#33).

        Read-only and best-effort: returns ``rmux_enabled=false`` (and no agents)
        when AIFactory's rmux console is off or the service is unreachable. Each
        agent carries a cockpit-side ``ws_path`` that the backend WS proxy (#34)
        re-streams from — the browser never sees an AIFactory URL or token."""
        ai = next((a for a in adapters if a.service is Service.AIFACTORY), None)
        try:
            if isinstance(ai, AIFactoryAdapter):
                result = await run_in_threadpool(discover_live_agents, ai)
            else:
                result = LiveAgentsResult(rmux_enabled=False, agents=[])
        finally:
            for adapter in adapters:
                adapter.close()
        return {
            "rmux_enabled": result.rmux_enabled,
            "count": len(result.agents),
            "agents": [a.model_dump(mode="json") for a in result.agents],
        }

    @app.websocket("/api/live-agents/{correlation_key}/ws")
    async def live_agent_ws(
        websocket: WebSocket,
        correlation_key: str,
        adapters: list[BaseHTTPAdapter] = Depends(adapters_dep),
        connect: ConnectFn = Depends(live_agent_connect_dep),
    ) -> None:
        """Stream one AIFactory agent's rmux console to the cockpit (#34).

        Read-only server-side proxy: resolves the correlation key to a spec_id,
        opens AIFactory's agent-console WS, and pumps ANSI bytes down. The
        AIFactory URL and token never leave this process."""
        if await _reject_unauthorized_ws(websocket):
            return
        ai = next((a for a in adapters if isinstance(a, AIFactoryAdapter)), None)
        try:
            if ai is None:
                await websocket.accept()
                await websocket.close(code=4404, reason="aifactory adapter unavailable")
                return
            await proxy_agent_console(websocket, correlation_key, ai, settings, connect=connect)
        finally:
            for adapter in adapters:
                adapter.close()

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

    @app.get("/api/workitems/{correlation_key}/process")
    async def get_process(
        correlation_key: str,
        store: WorkItemStore = Depends(store_dep),
        adapters: list[BaseHTTPAdapter] = Depends(adapters_dep),
    ) -> dict[str, object]:
        """Rich live process detail for the work item's code task (#45).

        Proxies + normalizes the active AIFactory task (phase, progress %, current
        subtask, subtask list) from its REST detail endpoint. Best-effort:
        ``available: false`` when there's no task or the service is unreachable."""
        try:
            return await run_in_threadpool(
                build_process_detail, store, adapters, correlation_key
            )
        finally:
            for adapter in adapters:
                adapter.close()

    @app.post("/api/actions/propose")
    async def propose_action(
        req: ProposeRequest, store: WorkItemStore = Depends(store_dep)
    ) -> PreparedAction:
        """Build (but do NOT execute) a PreparedAction for the given work item.

        Advise-only: this never touches an upstream service. 400 for an unknown
        kind; 404 if there's no work item for the correlation key."""
        try:
            action = await run_in_threadpool(propose, store, req.kind, req.correlation_key, req.note)
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

    @app.get("/api/activity")
    def list_activity(
        limit: int = 50, store: WorkItemStore = Depends(store_dep)
    ) -> dict[str, object]:
        """Live activity feed: completion events flattened across all work items,
        newest first. Read-only; powers the Audit page's Activity panel so it
        reflects real pipeline traffic even before any action is executed."""
        items = store.list()
        titles = {wi.correlation_key: wi.title for wi in items}
        events: list[dict[str, object]] = []
        for wi in items:
            for ev in wi.timeline:
                ts = ev.updated_at
                events.append(
                    {
                        "service": ev.service.value,
                        "correlation_key": ev.correlation_key,
                        "status": ev.status,
                        "phase": ev.phase,
                        "updated_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                        "title": titles.get(ev.correlation_key),
                    }
                )
        events.sort(key=lambda e: str(e["updated_at"]), reverse=True)
        return {"count": len(events), "activity": events[: max(0, limit)]}

    @app.post("/api/copilot/ask")
    async def copilot_ask(req: AskRequest, copilot: Copilot = Depends(copilot_dep)) -> dict[str, object]:
        result = await run_in_threadpool(copilot.ask, req.question)
        return {"answer": result.answer, "work_items_considered": result.work_items_considered}

    @app.get("/api/copilot/provider")
    async def copilot_provider() -> dict[str, object]:
        """Active copilot LLM provider (#59). For an OpenAI-compatible endpoint
        (e.g. Ollama Cloud) this probes {base}/models so the cockpit can confirm
        connectivity and list the available cloud models."""
        return await run_in_threadpool(provider_status, settings)

    def _settings_payload() -> dict[str, object]:
        status = provider_status(settings)
        return {"copilot": {**status, "providers": list(COPILOT_PROVIDERS)}}

    @app.get("/api/settings")
    async def get_settings_view() -> dict[str, object]:
        """Editable cockpit settings. Currently the copilot provider + model, plus
        the available providers and (for Ollama Cloud) live connectivity + model
        list. The API key is never returned — only a ``has_key`` flag."""
        return await run_in_threadpool(_settings_payload)

    @app.put("/api/settings/copilot")
    async def update_copilot_settings(
        update: CopilotSettingsUpdate,
        _scope: str | None = Depends(require_scope("write")),
    ) -> dict[str, object]:
        """Switch the copilot provider/model at runtime. Persisted to the workspace
        (provider + model only — never the key) so it survives a restart, and
        effective immediately (the copilot is rebuilt on the next question).
        Requires the ``write`` scope when API keys are configured."""
        try:
            await run_in_threadpool(set_copilot_settings, update.provider, update.model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        reset_copilot()
        return await run_in_threadpool(_settings_payload)

    @app.get("/api/settings/token")
    def get_connect_token(
        keystore: KeyStore = Depends(keystore_dep),
    ) -> dict[str, object]:
        """The CFactory API token to use from an editor / external client (#73).

        Returns the configured key (the one ``/connect/vscode`` hands back) so the
        ``/settings/token`` page can show it with a copy button. ``configured`` is
        false in OPEN mode (no ``CFACTORY_API_KEYS`` set), where there is no token to
        copy and the API accepts every request anyway. Reachable behind the same
        gate as the rest of the cockpit UI."""
        token = keystore.preferred_key()
        return {
            "token": token or "",
            "configured": token is not None,
            "connect_url": settings.public_api_url or "",
        }

    @app.get("/connect/vscode")
    def connect_vscode(
        redirect: str,
        state: str,
        keystore: KeyStore = Depends(keystore_dep),
    ):
        """One-click editor hand-off (#73): 302 to ``<redirect>?token=…&state=…``.

        The ``factory-vscode`` extension opens this with the editor's callback URI
        and an opaque ``state`` nonce; we validate the callback's scheme against the
        editor allow-list, look up the configured API token, and redirect back with
        the token and the echoed ``state``. A disallowed scheme is rejected (400) and
        no token is issued. CFactory has no in-app login — access is gated at the
        ingress, so a reachable request is treated as authenticated."""
        reason = validate_redirect(redirect)
        if reason is not None:
            return HTMLResponse(
                f"<h1>Connection rejected</h1><p>{reason}.</p>"
                "<p>The redirect target must be a known editor URI "
                "(vscode://, vscodium://, cursor://, windsurf://, …).</p>",
                status_code=400,
            )
        if not state.strip():
            return HTMLResponse(
                "<h1>Connection rejected</h1><p>missing state nonce.</p>",
                status_code=400,
            )
        token = keystore.preferred_key() or ""
        return RedirectResponse(build_redirect(redirect, token, state), status_code=302)

    async def _reject_unauthorized_ws(websocket: WebSocket) -> bool:
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

    @app.websocket("/api/ws")
    async def cockpit_feed(
        websocket: WebSocket, store: WorkItemStore = Depends(store_dep)
    ) -> None:
        if await _reject_unauthorized_ws(websocket):
            return
        await manager.connect(websocket)
        try:
            # Push the current state immediately so a fresh or *reconnecting*
            # client is up to date at once, rather than waiting for the next
            # poll-loop broadcast (which is what made reconnects look stale).
            snapshot = await run_in_threadpool(store.list)
            await websocket.send_json(
                {"type": "snapshot", "items": [wi.model_dump(mode="json") for wi in snapshot]}
            )
            while True:
                # Client sends periodic "ping" keepalives; we only need to drain
                # them. receive_text raises WebSocketDisconnect when it goes away.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(websocket)

    # Read-only MCP server at POST /mcp — the single PARR-pipeline visibility
    # surface for external agents (Claude Code, the /parr-run conductor).
    from .mcp import router as mcp_router

    app.include_router(mcp_router)

    return app


app = create_app()

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
from .adapters import (
    AdapterError,
    AIFactoryAdapter,
    BaseHTTPAdapter,
    ServiceProbe,
    build_adapters,
    hydrate,
)
from .config import get_settings, load_service_overrides, set_service_url
from .copilot import Copilot, get_copilot, provider_status
from .copilot.anomalies import detect_anomalies
from .copilot.tools import rollups as compute_rollups
from .copilot.tools import summarize_timeline, token_totals
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


class ServiceEndpointUpdate(BaseModel):
    url: str


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

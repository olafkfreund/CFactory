"""Live AIFactory agent discovery + the read-only rmux console WS proxy."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from starlette.concurrency import run_in_threadpool

from .adapters import AIFactoryAdapter, BaseHTTPAdapter
from .api_deps import adapters_dep, live_agent_connect_dep, reject_unauthorized_ws
from .config import get_settings
from .live_agent_proxy import ConnectFn, proxy_agent_console
from .live_agents import LiveAgentsResult, discover_live_agents
from .models import Service

router = APIRouter(tags=["live-agents"])


@router.get("/api/live-agents")
async def get_live_agents(
    adapters: Annotated[list[BaseHTTPAdapter], Depends(adapters_dep)],
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


@router.websocket("/api/live-agents/{correlation_key}/ws")
async def live_agent_ws(
    websocket: WebSocket,
    correlation_key: str,
    adapters: Annotated[list[BaseHTTPAdapter], Depends(adapters_dep)],
    connect: Annotated[ConnectFn, Depends(live_agent_connect_dep)],
) -> None:
    """Stream one AIFactory agent's rmux console to the cockpit (#34).

    Read-only server-side proxy: resolves the correlation key to a spec_id,
    opens AIFactory's agent-console WS, and pumps ANSI bytes down. The
    AIFactory URL and token never leave this process."""
    if await reject_unauthorized_ws(websocket):
        return
    ai = next((a for a in adapters if isinstance(a, AIFactoryAdapter)), None)
    try:
        if ai is None:
            await websocket.accept()
            await websocket.close(code=4404, reason="aifactory adapter unavailable")
            return
        settings = get_settings()
        await proxy_agent_console(websocket, correlation_key, ai, settings, connect=connect)
    finally:
        for adapter in adapters:
            adapter.close()

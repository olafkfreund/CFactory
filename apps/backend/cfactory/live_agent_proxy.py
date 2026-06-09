"""Live agent console proxy — server-side rmux WS re-stream (#34).

The cockpit connects to ``WS /api/live-agents/{correlation_key}/ws`` on *this*
backend. We resolve the correlation key to an AIFactory ``spec_id``, open the
upstream rmux console WebSocket
(``ws(s)://aifactory/api/tasks/{spec_id}/agent-console/ws``) server-side, and
pump its ANSI pane bytes down to the cockpit. The browser never learns the
AIFactory URL and never holds an AIFactory token — both stay in this process.

**Read-only by design:** we never POST ``/attach`` and never forward cockpit
keystrokes upstream. Frames from the cockpit are drained solely to detect
disconnect. The upstream's initial ``{"type":"connected"}`` control frame is
swallowed so only terminal output reaches xterm.js.

Best-effort: an unknown key, an rmux-off upstream, or a dropped upstream closes
the cockpit socket cleanly rather than raising.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import websockets
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from .adapters.aifactory import AIFactoryAdapter
from .adapters.base import AdapterError
from .config import Settings

log = logging.getLogger("cfactory.live_agent_proxy")

# WebSocket close codes (4000-4999 are application-defined).
WS_NO_AGENT = 4404      # no live agent for this correlation key
WS_UPSTREAM_DOWN = 1011  # upstream console unreachable / errored

# Injectable so tests can supply a fake upstream instead of a real socket.
ConnectFn = Callable[..., Any]


def resolve_spec_id(adapter: AIFactoryAdapter, correlation_key: str) -> str | None:
    """Map a cockpit correlation key back to the AIFactory ``spec_id``.

    The cockpit keys agents by correlation key (often a GitHub issue number); the
    upstream console is addressed by ``spec_id`` (the task id). We look it up in
    the same ``/api/tasks`` list discovery uses. Returns ``None`` if not found.
    """
    for item in adapter.list_items():
        if item.correlation_key == correlation_key:
            return item.task_id
    return None


def upstream_ws_url(settings: Settings, spec_id: str) -> str:
    """ws(s):// URL of the AIFactory agent console for ``spec_id``."""
    base = (
        settings.aifactory_api_url.replace("https://", "wss://")
        .replace("http://", "ws://")
        .rstrip("/")
    )
    return f"{base}/api/tasks/{quote(spec_id, safe='')}/agent-console/ws"


def _auth_headers(settings: Settings) -> dict[str, str]:
    token = settings.aifactory_token or settings.upstream_token
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def is_control_frame(raw: str | bytes) -> bool:
    """True for the upstream's JSON connection-control frame (not terminal data).

    Pane output arrives as binary; the only text JSON frame is the initial
    ``{"type":"connected","connection_id":...}`` handshake, which xterm must not
    render.
    """
    if isinstance(raw, (bytes, bytearray)):
        return False
    try:
        data: Any = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("type") == "connected"


async def _pump_upstream_to_cockpit(cockpit: WebSocket, upstream: Any) -> None:
    """Forward upstream pane frames to the cockpit, dropping control frames."""
    async for raw in upstream:
        if is_control_frame(raw):
            continue
        if isinstance(raw, (bytes, bytearray)):
            await cockpit.send_bytes(bytes(raw))
        else:
            await cockpit.send_text(raw)


async def _drain_cockpit(cockpit: WebSocket) -> None:
    """Read-only: consume (and discard) cockpit frames to detect disconnect."""
    try:
        while True:
            await cockpit.receive()
    except WebSocketDisconnect:
        return


async def proxy_agent_console(
    cockpit: WebSocket,
    correlation_key: str,
    adapter: AIFactoryAdapter,
    settings: Settings,
    *,
    connect: ConnectFn = websockets.connect,
) -> None:
    """Accept the cockpit socket and bridge it to the upstream rmux console."""
    await cockpit.accept()

    if not adapter.rmux_enabled():
        await _close(cockpit, WS_NO_AGENT, "live agents disabled upstream")
        return
    try:
        spec_id = await run_in_threadpool(resolve_spec_id, adapter, correlation_key)
    except AdapterError:
        spec_id = None
    if spec_id is None:
        await _close(cockpit, WS_NO_AGENT, "no live agent for key")
        return

    url = upstream_ws_url(settings, spec_id)
    try:
        async with connect(url, additional_headers=_auth_headers(settings)) as upstream:
            await _bridge(cockpit, upstream)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — upstream down/closed is expected, not fatal
        log.debug("live-agent upstream %s failed: %s", url, exc)
        await _close(cockpit, WS_UPSTREAM_DOWN, "upstream console closed")


async def _bridge(cockpit: WebSocket, upstream: Any) -> None:
    """Run both directions until either side ends, then tear down."""
    up = asyncio.create_task(_pump_upstream_to_cockpit(cockpit, upstream))
    down = asyncio.create_task(_drain_cockpit(cockpit))
    try:
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (up, down):
            if not task.done():
                task.cancel()
        await asyncio.gather(up, down, return_exceptions=True)
        await _close(cockpit, 1000, "session ended")


async def _close(cockpit: WebSocket, code: int, reason: str) -> None:
    """Close the cockpit socket if still open (idempotent)."""
    if cockpit.application_state is not WebSocketState.DISCONNECTED:
        try:
            await cockpit.close(code=code, reason=reason)
        except RuntimeError:
            pass  # already closing/closed

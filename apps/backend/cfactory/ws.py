"""WebSocket broadcast hub — pushes WorkItem updates to connected cockpits (#10)."""

from __future__ import annotations

from functools import cache
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Tracks connected cockpit clients and fans out JSON messages to them."""

    def __init__(self) -> None:
        self._active: dict[WebSocket, str | None] = {}

    async def connect(self, websocket: WebSocket, tenant: str | None = None) -> None:
        await websocket.accept()
        self._active[websocket] = tenant

    def disconnect(self, websocket: WebSocket) -> None:
        self._active.pop(websocket, None)

    @property
    def count(self) -> int:
        return len(self._active)

    async def broadcast(self, message: dict[str, Any], tenant: str | None = None) -> None:
        """Fan out to every client, or only to clients of ``tenant`` when it is set."""
        dead: list[WebSocket] = []
        for ws, ws_tenant in list(self._active.items()):
            if tenant is not None and ws_tenant != tenant:
                continue
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001 — a broken client must not break the fan-out
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


@cache
def get_manager() -> ConnectionManager:
    return ConnectionManager()

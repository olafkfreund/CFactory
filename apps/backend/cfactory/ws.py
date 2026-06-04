"""WebSocket broadcast hub — pushes WorkItem updates to connected cockpits (#10)."""

from __future__ import annotations

from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Tracks connected cockpit clients and fans out JSON messages to them."""

    def __init__(self) -> None:
        self._active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._active.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._active.discard(websocket)

    @property
    def count(self) -> int:
        return len(self._active)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._active):
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001 — a broken client must not break the fan-out
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_manager: ConnectionManager | None = None


def get_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager

"""The cockpit live-feed WebSocket: pushes the current snapshot on connect, then
keeps the socket open for broadcast pushes from the shared manager.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from .api_deps import reject_unauthorized_ws, store_dep
from .store import WorkItemStore
from .ws import get_manager

router = APIRouter(tags=["ws"])


@router.websocket("/api/ws")
async def cockpit_feed(
    websocket: WebSocket,
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> None:
    if await reject_unauthorized_ws(websocket):
        return
    manager = get_manager()
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

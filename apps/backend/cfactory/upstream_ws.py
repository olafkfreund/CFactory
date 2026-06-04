"""Upstream WebSocket subscriber (#10).

Connects to each service's live feed, parses messages into CompletionEvents,
upserts the store and rebroadcasts to connected cockpits. Best-effort: a down
service is retried with backoff and never crashes the app.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import websockets
from starlette.concurrency import run_in_threadpool

from .adapters.base import first
from .config import Settings, get_settings
from .models import CompletionEvent, Service
from .store import WorkItemStore
from .ws import ConnectionManager

log = logging.getLogger("cfactory.upstream_ws")


def parse_upstream_message(service: Service, raw: str | bytes) -> CompletionEvent | None:
    """Best-effort parse of a service's live message into a CompletionEvent."""
    try:
        data: Any = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    key = first(data, "correlation_key", "github_issue", "githubIssueNumber",
                "issue_number", "metadata.githubIssueNumber", "task_id", "id")
    task_id = first(data, "task_id", "id", "spec_id", "session_id")
    status = first(data, "status", "board_state")
    if key is None or task_id is None or status is None:
        return None

    raw_ts = first(data, "updated_at")
    try:
        ts = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else datetime.now(timezone.utc)
    except ValueError:
        ts = datetime.now(timezone.utc)

    return CompletionEvent(
        correlation_key=str(key),
        service=service,
        task_id=str(task_id),
        status=str(status),
        phase=first(data, "phase"),
        updated_at=ts,
    )


async def handle_message(
    service: Service, raw: str | bytes, store: WorkItemStore, manager: ConnectionManager
) -> CompletionEvent | None:
    """Parse one upstream message, persist it, and rebroadcast to cockpits."""
    event = parse_upstream_message(service, raw)
    if event is None:
        return None
    work_item = await run_in_threadpool(store.upsert_from_event, event)
    await manager.broadcast({"type": "workitem", "item": work_item.model_dump(mode="json")})
    return event


async def consume_once(
    ws_url: str, service: Service, store: WorkItemStore, manager: ConnectionManager
) -> CompletionEvent | None:
    """Connect, handle a single message, disconnect (used by tests)."""
    async with websockets.connect(ws_url) as ws:
        return await handle_message(service, await ws.recv(), store, manager)


async def subscribe(
    service: Service,
    ws_url: str,
    store: WorkItemStore,
    manager: ConnectionManager,
    *,
    retry_delay: float = 3.0,
) -> None:
    """Long-lived subscription with reconnect/backoff."""
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                log.info("subscribed to %s at %s", service.value, ws_url)
                async for raw in ws:
                    await handle_message(service, raw, store, manager)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep retrying a flaky/down upstream
            log.debug("subscription to %s failed (%s); retrying in %ss", service.value, exc, retry_delay)
            await asyncio.sleep(retry_delay)


def start_subscribers(
    store: WorkItemStore, manager: ConnectionManager, settings: Settings | None = None
) -> list[asyncio.Task[None]]:
    """Spawn one subscriber task per service. Caller cancels them on shutdown."""
    settings = settings or get_settings()
    tasks: list[asyncio.Task[None]] = []
    for name, url in settings.upstream_ws_urls().items():
        tasks.append(asyncio.create_task(subscribe(Service(name), url, store, manager)))
    return tasks

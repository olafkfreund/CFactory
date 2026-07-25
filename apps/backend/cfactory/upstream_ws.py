"""Upstream WebSocket subscriber (#10).

Connects to each service's live feed, parses messages into CompletionEvents,
upserts the store and rebroadcasts to connected cockpits. Best-effort: a down
service is retried with backoff and never crashes the app.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from functools import partial
from typing import Any

import websockets
from starlette.concurrency import run_in_threadpool

from . import card_ops
from .adapters.base import first
from .audit import get_audit_store
from .card_intake import apply_status
from .cards import get_cards_store
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

    key = first(
        data,
        "correlation_key",
        "github_issue",
        "githubIssueNumber",
        "issue_number",
        "metadata.githubIssueNumber",
        "task_id",
        "id",
    )
    task_id = first(data, "task_id", "id", "spec_id", "session_id")
    status = first(data, "status", "board_state")
    if key is None or task_id is None or status is None:
        return None

    raw_ts = first(data, "updated_at")
    try:
        ts = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else datetime.now(UTC)
    except ValueError:
        ts = datetime.now(UTC)

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
    work_item, applied = await run_in_threadpool(store.upsert_from_event, event)
    # Idempotent: a duplicate upstream message is not re-broadcast.
    if applied:
        # Same card write-back as the REST ingress (RFC-0019 §3.2) — a card must
        # not go stale just because its progress arrived over the WS relay
        # instead of /api/events. Includes advancing an explicitly-driven stage
        # sequence (RFC-0020 §3.7), for the same reason: which ingress the event
        # arrived on must not decide whether the next stage runs. Unscoped store
        # and default transport: a relayed upstream message carries no tenant
        # header to scope by and no test seam to inject.
        ctx = card_ops.AuditContext(
            get_audit_store(), card_ops.SEQUENCE_ACTOR, endpoint="/ws/upstream"
        )
        await run_in_threadpool(
            partial(
                apply_status,
                get_cards_store(),
                work_item,
                on_dispatch=card_ops.dispatch_recorder(ctx),
            )
        )
        await manager.broadcast({"type": "workitem", "item": work_item.model_dump(mode="json")})
    return event


def _auth_headers(settings: Settings) -> dict[str, str]:
    """Bearer header for the upstream WS feeds, matching the factories' REST auth.
    Empty when no token is configured (local dev with auth disabled)."""
    if settings.upstream_token:
        return {"Authorization": f"Bearer {settings.upstream_token}"}
    return {}


async def subscribe(
    service: Service,
    ws_url: str,
    store: WorkItemStore,
    manager: ConnectionManager,
    *,
    retry_delay: float = 3.0,
    headers: dict[str, str] | None = None,
) -> None:
    """Long-lived subscription with reconnect/backoff."""
    while True:
        try:
            async with websockets.connect(ws_url, additional_headers=headers or {}) as ws:
                log.info("subscribed to %s at %s", service.value, ws_url)
                async for raw in ws:
                    await handle_message(service, raw, store, manager)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep retrying a flaky/down upstream
            log.debug(
                "subscription to %s failed (%s); retrying in %ss", service.value, exc, retry_delay
            )
            await asyncio.sleep(retry_delay)


def start_subscribers(
    store: WorkItemStore, manager: ConnectionManager, settings: Settings | None = None
) -> list[asyncio.Task[None]]:
    """Spawn one subscriber task per service. Caller cancels them on shutdown."""
    settings = settings or get_settings()
    headers = _auth_headers(settings)
    tasks: list[asyncio.Task[None]] = []
    for name, url in settings.upstream_ws_urls().items():
        tasks.append(
            asyncio.create_task(subscribe(Service(name), url, store, manager, headers=headers))
        )
    return tasks

"""Live progress (#v2 P3).

CFactory's WorkItem store only holds terminal completion events. To animate the
cockpit live, this module gathers coarse live progress — current stage/phase, and
a real % when AIFactory streams it — into an in-memory hub, broadcast over the
existing /api/ws as `{type:"progress"}` and served by GET /api/progress.

Pure mappers + the hub are unit-tested. The live feeders (AIFactory progress-WS
subscriber + PFactory/TFactory pollers) are gated by CFACTORY_LIVE_PROGRESS and
verified on a host with the siblings running.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .adapters import AdapterError, AdapterItem, BaseHTTPAdapter, build_adapters, hydrate
from .config import Settings, get_settings
from .models import Service
from .store import WorkItemStore, get_store
from .ws import ConnectionManager


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LiveProgress(BaseModel):
    correlation_key: str
    service: Service
    phase: str | None = None
    percent: float | None = None      # 0..100 when known (AIFactory); else None = indeterminate
    subtask: str | None = None
    updated_at: datetime


def progress_from_item(item: AdapterItem) -> LiveProgress:
    """Coarse progress from a service list item (stage/phase, no %)."""
    return LiveProgress(
        correlation_key=item.correlation_key,
        service=item.service,
        phase=item.phase or item.status,
        percent=None,
        updated_at=_now(),
    )


def progress_from_aifactory_frame(frame: dict[str, Any], correlation_key: str) -> LiveProgress:
    """Fine progress from an AIFactory /ws/progress frame (phase + percentage)."""
    pct = frame.get("percentage")
    return LiveProgress(
        correlation_key=correlation_key,
        service=Service.AIFACTORY,
        phase=frame.get("phase"),
        percent=float(pct) if pct is not None else None,
        subtask=frame.get("subtask"),
        updated_at=_now(),
    )


class LiveProgressHub:
    """Latest live progress per correlation key (most recently active stage wins)."""

    def __init__(self) -> None:
        self._items: dict[str, LiveProgress] = {}

    def update(self, lp: LiveProgress) -> None:
        self._items[lp.correlation_key] = lp

    def snapshot(self) -> list[LiveProgress]:
        return sorted(self._items.values(), key=lambda p: p.updated_at, reverse=True)


_hub: LiveProgressHub | None = None


def get_progress_hub() -> LiveProgressHub:
    global _hub
    if _hub is None:
        _hub = LiveProgressHub()
    return _hub


def reset_progress_hub() -> None:
    global _hub
    _hub = None


def poll_progress_once(
    hub: LiveProgressHub,
    adapters: list[BaseHTTPAdapter],
    store: WorkItemStore | None = None,
) -> list[LiveProgress]:
    """Poll each adapter's list once, fold coarse progress into the hub.

    When a ``store`` is given, also hydrate it from the same fetch and reconcile
    away stale non-terminal stages the upstream no longer reports — so the board
    self-heals (finished/removed tasks stop showing as running) without a manual
    refresh. Best-effort: a down service is skipped and never reconciled."""
    changed: list[LiveProgress] = []
    for adapter in adapters:
        try:
            items = adapter.list_items()
            for item in items:
                lp = progress_from_item(item)
                hub.update(lp)
                changed.append(lp)
            if store is not None:
                hydrate(store, items)
                store.reconcile_snapshot(adapter.service, {i.task_id for i in items})
        except AdapterError:
            pass
        finally:
            adapter.close()
    return changed


async def _poll_loop(
    hub: LiveProgressHub, manager: ConnectionManager, settings: Settings, interval: float
) -> None:
    store = get_store()
    while True:
        try:
            changed = await run_in_threadpool(
                poll_progress_once, hub, build_adapters(settings), store
            )
            for lp in changed:
                await manager.broadcast({"type": "progress", "item": lp.model_dump(mode="json")})
            # Push the reconciled board so cleared/updated stages reach the cockpit
            # live (stale "running" ghosts disappear without a manual refresh).
            snapshot = await run_in_threadpool(store.list)
            await manager.broadcast(
                {"type": "snapshot", "items": [wi.model_dump(mode="json") for wi in snapshot]}
            )
        except Exception:  # noqa: BLE001 — best-effort, never crash the cockpit
            pass
        await asyncio.sleep(interval)


def start_progress(
    hub: LiveProgressHub, manager: ConnectionManager, settings: Settings | None = None
) -> list[asyncio.Task[None]]:
    """Gated by CFACTORY_LIVE_PROGRESS. Spawns the P/T poll loop (coarse). The
    AIFactory progress-WS subscriber is added when run against a live AIFactory."""
    settings = settings or get_settings()
    if not settings.live_progress:
        return []
    return [asyncio.create_task(_poll_loop(hub, manager, settings, settings.poll_interval_seconds))]


async def stop_progress(tasks: list[asyncio.Task[None]]) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t

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
import logging
from datetime import UTC, datetime
from functools import cache
from typing import Any

from factory_common.logsafe import sanitize_log
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .adapters import AdapterError, AdapterItem, BaseHTTPAdapter, build_adapters, hydrate
from .config import Settings, get_settings
from .models import Service
from .status_taxonomy import is_dead_stage as _is_dead_stage
from .store import WorkItemStore, get_store
from .ws import ConnectionManager

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class LiveProgress(BaseModel):
    correlation_key: str
    service: Service
    phase: str | None = None
    percent: float | None = None  # 0..100 when known (AIFactory); else None = indeterminate
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


@cache
def get_progress_hub() -> LiveProgressHub:
    return LiveProgressHub()


def reset_progress_hub() -> None:
    get_progress_hub.cache_clear()


def _created(item: AdapterItem) -> datetime | None:
    """The item's creation time, UTC when naive; None when absent or unparseable."""
    try:
        t = datetime.fromisoformat(item.created_at or "")
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _current_task(store: WorkItemStore, item: AdapterItem) -> str | None:
    """The task id the row for ``item``'s key currently shows for its service."""
    wi = store.get(item.correlation_key)
    return getattr(wi, item.service.value).task_id if wi is not None else None


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
            # A stage the upstream reports as stuck/stalled is a dead task — its
            # own watchdog gave up. Keep it out of the live feed so it never reads
            # as "running"; it stays in the store, stored as stalled.
            stuck_ids = {i.task_id for i in items if _is_dead_stage(i.status)}
            for item in items:
                if item.task_id in stuck_ids:
                    continue
                lp = progress_from_item(item)
                hub.update(lp)
                changed.append(lp)
            if store is not None:
                # Tasks sharing a key share one row. When every task on a key has
                # a creation time, the newest one owns the row, dead or alive
                # (#484). Otherwise a dead task is hydrated only when its key has
                # no live task (a re-run), and then only the last dead one listed,
                # so two writers never flip the row every poll (#483).
                groups: dict[str, list[AdapterItem]] = {}
                for i in items:
                    groups.setdefault(i.correlation_key, []).append(i)
                owners: list[AdapterItem] = []
                rest: list[AdapterItem] = []
                for group in groups.values():
                    stamped = [(t, i) for i in group if (t := _created(i)) is not None]
                    if len(stamped) == len(group):
                        owners.append(max(stamped, key=lambda p: p[0])[1])
                    else:
                        rest.extend(group)
                alive_keys = {i.correlation_key for i in rest if i.task_id not in stuck_ids}
                last_dead = {i.correlation_key: i.task_id for i in rest if i.task_id in stuck_ids}
                live = owners + [
                    i
                    for i in rest
                    if i.task_id not in stuck_ids
                    or (
                        i.correlation_key not in alive_keys
                        and last_dead[i.correlation_key] == i.task_id
                    )
                ]
                # "Newest" is only known among this poll's tasks: it may override
                # #483's guard only when the row's current task is one of them, or
                # a lone dead task would beat an unlisted (event-only) re-run.
                newest_ids = frozenset(
                    o.task_id
                    for o in owners
                    if _current_task(store, o) in {i.task_id for i in groups[o.correlation_key]}
                )
                hydrate(store, live, newest_ids=newest_ids)
                store.reconcile_snapshot(adapter.service, {i.task_id for i in live})
                # Drop orphaned duplicate cards left when an upstream re-keys a
                # task (fallback id -> real GitHub-issue key); reconcile preserves
                # their terminal stage, so prune by task_id->canonical-key here.
                store.prune_duplicate_stages(
                    adapter.service, {i.task_id: i.correlation_key for i in live}
                )
        except AdapterError as exc:
            # Best-effort per docstring: a down service is skipped and never
            # reconciled this cycle rather than taking the whole poll down.
            logger.warning(
                "[cfactory] progress poll skipped service=%s error=%s",
                adapter.service,
                sanitize_log(str(exc)),
            )
        finally:
            adapter.close()
    if store is not None:
        # Once per cycle, after every adapter: a dead stage stays visible for a
        # while (24 h by default), then its row is removed (#479, #187).
        store.prune_dead()
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
        except Exception as exc:  # noqa: BLE001 — best-effort, never crash the cockpit
            logger.debug("live-progress broadcast failed: %s", type(exc).__name__)
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

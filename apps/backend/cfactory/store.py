"""Persistence for WorkItems — the cross-service correlation store.

A WorkItem is keyed by its correlation key (the GitHub issue number) and holds a
per-service slice (plan / code / test) plus an ordered event timeline. Completion
events upsert the matching slice and append to the timeline.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, Select, String, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from .config import DEFAULT_TENANT, Settings, get_settings
from .db import Base, make_engine
from .models import CompletionEvent, Liveness, Service, ServiceState, WorkItem
from .status_taxonomy import is_terminal as _is_terminal
from .usage import add_usage, empty_bucket

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


# Idle budget before an active (non-terminal) stage is flagged stalled (#105).
# Generous by default — real stages can be legitimately quiet (LLM calls, Docker,
# stability re-runs); mirrors TFactory's watchdog default. Env-tunable.
_DEFAULT_STALL_DEADLINE_SECONDS = 900.0
_PIPELINE_ORDER = ("pfactory", "aifactory", "tfactory")


def stall_deadline_seconds() -> float:
    """The stall deadline in seconds (env ``CFACTORY_STALL_DEADLINE_SECONDS``)."""
    import os

    raw = os.environ.get("CFACTORY_STALL_DEADLINE_SECONDS")
    if raw is None:
        return _DEFAULT_STALL_DEADLINE_SECONDS
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_STALL_DEADLINE_SECONDS
    return val if val > 0 else _DEFAULT_STALL_DEADLINE_SECONDS


def _as_aware(dt: datetime | None) -> datetime | None:
    """Treat a naive timestamp (SQLite drops tz) as UTC so subtraction is safe."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def compute_liveness(
    item: WorkItem,
    *,
    now: datetime | None = None,
    deadline_seconds: float | None = None,
) -> Liveness:
    """Compute a WorkItem's liveness signal (RFC-0008 §3.4, #105).

    The *active frontier* is the furthest-along service (pfactory → aifactory →
    tfactory) that has a status; the item is "active" only when that frontier
    status is non-terminal (an agent claims to be working). ``stalled`` is True
    when it's active but ``updated_at`` hasn't moved within ``deadline_seconds``
    — the silent 'reviewing' hang from the taskboard demo. An item between
    stages (frontier terminal, downstream not started) is NOT flagged, to keep
    false positives out of the watch plane.
    """
    now = now or _now()
    deadline = stall_deadline_seconds() if deadline_seconds is None else deadline_seconds
    updated = _as_aware(item.updated_at)
    age = (now - updated).total_seconds() if updated is not None else 0.0

    last_service: str | None = None
    last_status: str | None = None
    for svc in _PIPELINE_ORDER:
        status = getattr(item, svc).status
        if status:
            last_service, last_status = svc, status

    active = last_status is not None and not _is_terminal(last_status)
    stalled = active and updated is not None and age > deadline
    return Liveness(
        last_activity_age_seconds=round(age, 1),
        active_service=last_service if active else None,
        active_status=last_status if active else None,
        stalled=stalled,
        deadline_seconds=deadline,
    )


def _row_has_stage_data(row: WorkItemRow) -> bool:
    """True if any of the three service stages carries a status or task_id —
    i.e. the work item still represents real upstream state."""
    for svc in ("pfactory", "aifactory", "tfactory"):
        data = getattr(row, svc, None) or {}
        if data.get("status") or data.get("task_id"):
            return True
    return False


def _is_worker_event(event: CompletionEvent) -> bool:
    """True for a v1.3 live per-worker sub-event (``phase:"worker"`` + worker)."""
    return event.phase == "worker" and event.worker is not None


def _is_worker_progress_event(event: CompletionEvent) -> bool:
    """True for a Tier-1.5 heartbeat sample (``phase:"worker_progress"`` + worker).

    A stream sample appended to the rolling ``worker_progress`` series — it never
    mutates the scalar service slice (it is not terminal)."""
    return event.phase == "worker_progress" and event.worker is not None


# Cap the rolling per-worker progress series. A ~10s heartbeat over a long build
# would otherwise grow unbounded; keeping the last ~120 points (~20 min at 10s)
# bounds store growth while leaving plenty for a smooth sparkline. The series is
# only needed WHILE running and is pruned entirely on a terminal event.
_PROGRESS_SERIES_CAP = 120


def _coerce_ts_ms(event: CompletionEvent) -> int:
    """Best-effort epoch-ms for a heartbeat sample, from the event ``updated_at``.

    Gives the series a monotone-ish x-axis for the sparkline even when individual
    heartbeats don't carry their own timestamp. Never raises."""
    try:
        return int(event.updated_at.timestamp() * 1000)
    except (AttributeError, ValueError, OSError):  # pragma: no cover - defensive
        return 0


def rollup_by(workers: dict | None, dim: str) -> dict[str, dict]:
    """Roll a ``worker_id -> worker`` map up by a dimension (``provider``/``model``).

    Returns ``{dim_value: {tokens..., cost_usd, workers: <count>}}``. Workers
    with a null/empty dimension value bucket under ``"unknown"``. Pure +
    side-effect-free so the API can recompute it on read.
    """
    out: dict[str, dict] = {}
    for w in (workers or {}).values():
        wd = w if isinstance(w, dict) else w.model_dump()
        key = wd.get(dim) or "unknown"
        b = out.setdefault(key, empty_bucket())
        add_usage(b, wd, count_worker=True)
        b["duration_ms"] = b.get("duration_ms", 0) + (wd.get("duration_ms", 0) or 0)
        # A provider bucket shares one billing mode (#96); carry it when present
        # (live worker sub-events may omit it — then it stays absent until the
        # terminal by_provider rollup supplies it).
        if wd.get("billing_mode"):
            b.setdefault("billing_mode", wd.get("billing_mode"))
    return out


def _already_recorded(timeline: list | None, event: CompletionEvent) -> bool:
    """True if this event is already in the timeline (idempotency check).

    Live per-worker sub-events (v1.3, ``phase:"worker"``) dedup on
    ``(service, correlation_key, worker_id)`` — every worker shares the same
    ``status`` ("worker_done"), so the legacy ``(service, status)`` key would
    wrongly collide across distinct workers. A re-emit of the *same* worker_id
    is a no-op for the timeline (the slice still upserts/replaces — no double
    count). When the producer also stamps a per-event ``id`` we honour it first
    (re-delivery → no-op).

    For non-worker events the per-event envelope ``id`` (#468 — AIFactory #466 /
    TFactory #282) is the SOLE dedup key (#471 cutover): exactly-once on the ``id``
    makes the outbox relay's re-delivery a no-op *and* lets a legitimate re-run
    after handback through (same service + status but a new ``id``). The legacy
    ``(service, correlation_key, status)`` fallback was removed — every producer
    now stamps an ``id``, and that key wrongly collided on legitimate re-runs. A
    non-worker event WITHOUT an ``id`` is an ingest anomaly (logged), recorded
    without id-dedup rather than silently collapsed onto the removed legacy key.
    """
    entries = timeline or []
    # Heartbeat samples (``phase:"worker_progress"``) are a high-frequency STREAM:
    # every ~10s tick is a distinct, wanted sample. They are never deduped here
    # (and are not appended to the timeline at all — see upsert_from_event), so
    # the legacy ``(service, status="running")`` key can't collapse them.
    if _is_worker_progress_event(event):
        return False
    if event.id:
        return any(e.get("id") == event.id for e in entries)
    if _is_worker_event(event):
        wid = event.worker.worker_id
        return any(
            e.get("service") == event.service.value
            and (e.get("worker") or {}).get("worker_id") == wid
            for e in entries
        )
    # #471 cutover: the CloudEvents ``id`` is the only dedup key now. A non-worker
    # event without one is an ingest anomaly — every producer stamps an ``id``, so
    # this means a producer regressed or a malformed event slipped through. Surface
    # it (don't silently fall back to the removed legacy key) and record it anyway
    # (return False) — dropping a real terminal event is worse than a missed dedup.
    # Grep marker: "missing CloudEvents id".
    logger.error(
        "[#471] ingest anomaly: completion event missing CloudEvents id — "
        "service=%s correlation_key=%s status=%s; recording without id-dedup",
        event.service.value,
        event.correlation_key,
        event.status,
    )
    return False


def _apply_worker_progress(prev: dict, event: CompletionEvent) -> dict:
    """Build the slice for a Tier-1.5 heartbeat (``phase:"worker_progress"``).

    APPENDS a ProgressPoint to the rolling per-worker series, capped at the last
    ``_PROGRESS_SERIES_CAP`` points. Does NOT touch the service slice's
    status/phase/usage/workers — it's a stream, not a terminal record (and the
    caller appends no timeline entry; the series IS the record).
    """
    w = event.worker
    series_map = dict(prev.get("worker_progress") or {})
    series = list(series_map.get(w.worker_id) or [])
    series.append(
        {
            "ts": _coerce_ts_ms(event),
            "total_tokens": w.total_tokens,
            "cost_usd": w.cost_usd,
            "elapsed_ms": w.elapsed_ms,
        }
    )
    if len(series) > _PROGRESS_SERIES_CAP:
        series = series[-_PROGRESS_SERIES_CAP:]
    series_map[w.worker_id] = series
    slice_dict = {**prev, "worker_progress": series_map}
    # task_id is handy for the running-task progress API but never overwrites an
    # existing one.
    slice_dict.setdefault("task_id", event.task_id)
    return slice_dict


def _apply_worker(prev: dict, event: CompletionEvent) -> dict:
    """Build the slice for a live per-worker sub-event (``phase:"worker"``).

    Upserts into the slice's ``workers`` map (idempotent by worker_id — a re-emit
    replaces, no double count) WITHOUT touching the service-level
    usage/status/phase, then recomputes the provider/model rollups from the
    resulting map.
    """
    workers = dict(prev.get("workers") or {})
    workers[event.worker.worker_id] = event.worker.model_dump()
    return {
        **prev,
        "task_id": prev.get("task_id") or event.task_id,
        "workers": workers,
        "by_provider": rollup_by(workers, "provider"),
        "by_model": rollup_by(workers, "model"),
    }


def _apply_terminal_or_scalar(prev: dict, event: CompletionEvent) -> dict:
    """Build the slice for an ordinary / terminal event.

    Refreshes the scalar slice but PRESERVES any per-worker map accumulated from
    live sub-events. A terminal event that carries its own
    ``usage.workers``/``by_provider``/``by_model`` seeds the slice too (terminal
    rollup), recomputing rollups from the merged worker map for consistency. The
    rolling progress series is preserved while running and pruned on a terminal
    event so finished tasks don't bloat the store.
    """
    slice_dict = ServiceState(
        task_id=event.task_id,
        status=event.status,
        phase=event.phase,
        usage=event.usage,
    ).model_dump()
    # Cost is last-known-good: a plain status/phase event (no usage of its own —
    # e.g. a human_review or failed transition) must NOT clobber the usage already
    # recorded from a live snapshot, else the cockpit drops a real figure back to
    # zero. Carry the previous slice's usage forward when this event omits it.
    if event.usage is None and prev.get("usage"):
        slice_dict["usage"] = prev["usage"]
    workers = dict(prev.get("workers") or {})
    if event.usage and event.usage.workers:
        for w in event.usage.workers:
            workers[w.worker_id] = w.model_dump()
    slice_dict["workers"] = workers
    # Prefer the terminal event's explicit breakdowns when present; otherwise
    # derive from the worker map.
    if event.usage and event.usage.by_provider:
        slice_dict["by_provider"] = event.usage.by_provider
    else:
        slice_dict["by_provider"] = rollup_by(workers, "provider")
    if event.usage and event.usage.by_model:
        slice_dict["by_model"] = event.usage.by_model
    else:
        slice_dict["by_model"] = rollup_by(workers, "model")
    if _is_terminal(event.status):
        slice_dict["worker_progress"] = {}
    else:
        slice_dict["worker_progress"] = prev.get("worker_progress") or {}
    return slice_dict


# Optional per-event annotation blocks carried onto the service slice's
# ``extra`` map, all following the same additive pattern: present on the event ->
# stored under the same key; absent -> the slice is untouched. access = RFC-0007
# (#88) credentialed-lane honesty; verification = RFC-0006 (#76) gate-normalized
# VAL block; routing = RFC-0014 (#124) cost-aware routing decision (now incl.
# the Factory#272 tier/tier_source/savings_usd); injection_scan (Factory#273),
# dependency_review (TFactory#650) and votes (TFactory#649) = the #167
# security-gate / judge-vote verdicts.
_ANNOTATION_FIELDS = (
    "access",
    "verification",
    "routing",
    "injection_scan",
    "dependency_review",
    "votes",
)


def _attach_access_verification(slice_dict: dict, event: CompletionEvent) -> dict:
    """Carry the optional per-event annotation blocks onto a slice.

    Every ``_ANNOTATION_FIELDS`` block present on the event lands under the
    slice's ``extra`` map; absent blocks leave the slice untouched, so legacy
    envelopes round-trip unchanged. Returns the slice for chaining.
    """
    extra = dict(slice_dict.get("extra") or {})
    for field in _ANNOTATION_FIELDS:
        value = getattr(event, field, None)
        if value:
            # Pydantic model on the event (e.g. routing); persist JSON-safe.
            extra[field] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    if extra:
        slice_dict["extra"] = extra
    return slice_dict


class WorkItemRow(Base):
    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # Tenant partition (#172). Always "default" in single-tenant mode; stamped
    # from the resolved request tenant when CFACTORY_MULTI_TENANT is on.
    # ponytail: correlation_key stays globally unique — a cross-tenant key
    # collision on write raises IntegrityError; move to a (tenant_id,
    # correlation_key) composite unique if tenants ever share key spaces.
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT, index=True
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pfactory: Mapped[dict] = mapped_column(JSON, default=dict)
    aifactory: Mapped[dict] = mapped_column(JSON, default=dict)
    tfactory: Mapped[dict] = mapped_column(JSON, default=dict)
    timeline: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_model(self) -> WorkItem:
        return WorkItem(
            correlation_key=self.correlation_key,
            title=self.title,
            pfactory=ServiceState(**(self.pfactory or {})),
            aifactory=ServiceState(**(self.aifactory or {})),
            tfactory=ServiceState(**(self.tfactory or {})),
            timeline=[CompletionEvent(**e) for e in (self.timeline or [])],
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


def _ensure_tenant_column(engine: Engine) -> None:
    """Idempotent live-DB guard for the #172 ``tenant_id`` column.

    The deployed store bootstraps via ``create_all`` (no alembic step in the
    container), and ``create_all`` never ALTERs an existing table — so a DB
    created before #172 would crash every SELECT. The formal Alembic migration
    exists too; this guard covers deploys that don't run it. The DEFAULT
    backfills existing rows to the single ``default`` tenant.
    """
    from sqlalchemy import inspect, text  # noqa: PLC0415 — init-time only

    if "tenant_id" in {c["name"] for c in inspect(engine).get_columns("work_items")}:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE work_items ADD COLUMN tenant_id VARCHAR(64) "
                f"NOT NULL DEFAULT '{DEFAULT_TENANT}'"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_work_items_tenant_id ON work_items (tenant_id)")
        )


class WorkItemStore:
    """Thin repository over the work_items table.

    Pass an explicit ``url`` for tests (a temp SQLite file); production resolves
    from settings (PostgreSQL). Tables are created on init for dev/test; real
    deployments manage schema via Alembic.

    Tenant scoping (#172): an unscoped store (the default) sees every row —
    exactly the pre-#172 behaviour. ``scoped(tenant)`` returns a view whose
    reads filter by ``tenant_id`` and whose writes stamp it; ``store_dep``
    hands routes a scoped view only when CFACTORY_MULTI_TENANT is on.
    """

    def __init__(self, url: str | None = None, *, create: bool = True) -> None:
        self._engine = make_engine(url)
        self._session = sessionmaker(self._engine, expire_on_commit=False)
        self._tenant: str | None = None  # None = unscoped (single-tenant mode)
        if create:
            Base.metadata.create_all(self._engine)
            _ensure_tenant_column(self._engine)

    def scoped(self, tenant: str) -> WorkItemStore:
        """A tenant-scoped view sharing this store's engine/session factory."""
        import copy  # noqa: PLC0415 — trivial stdlib, scoped-view only

        view = copy.copy(self)
        view._tenant = tenant
        return view

    def _select(self) -> Select[tuple[WorkItemRow]]:
        """Base SELECT honouring the tenant scope (no filter when unscoped)."""
        stmt = select(WorkItemRow)
        if self._tenant is not None:
            stmt = stmt.where(WorkItemRow.tenant_id == self._tenant)
        return stmt

    def upsert_from_event(self, event: CompletionEvent) -> tuple[WorkItem, bool]:
        """Thread a completion event into its WorkItem.

        Idempotent by the envelope ``id`` (#471 cutover; see
        ``_already_recorded``): a duplicate delivery is a no-op — the existing
        item is returned with ``applied=False`` and the timeline is left
        untouched, so retried or relay-replayed deliveries don't double-count.
        A non-worker event without an ``id`` is logged as an ingest anomaly and
        recorded without id-dedup rather than collapsed onto the removed legacy
        key. Returns ``(work_item, applied)``.
        """
        # Two writers (poll loop + event ingest) can both miss the row and race
        # to INSERT the same key. On the resulting IntegrityError, retry once —
        # the row now exists, so the second pass takes the update path.
        for attempt in range(2):
            try:
                with self._session.begin() as session:
                    row = self._get_row(session, event.correlation_key)
                    if row is not None and _already_recorded(row.timeline, event):
                        return row.to_model(), False

                    if row is None:
                        row = WorkItemRow(
                            correlation_key=event.correlation_key,
                            timeline=[],
                            tenant_id=self._tenant or DEFAULT_TENANT,
                        )
                        session.add(row)

                    prev = getattr(row, event.service.value) or {}
                    if _is_worker_progress_event(event):
                        # A heartbeat is a high-frequency STREAM: it updates the
                        # rolling series only and appends NO timeline entry, so it
                        # returns early before the shared slice/timeline write.
                        setattr(row, event.service.value, _apply_worker_progress(prev, event))
                        row.updated_at = _now()
                        session.flush()
                        return row.to_model(), True

                    if _is_worker_event(event):
                        slice_dict = _apply_worker(prev, event)
                    else:
                        slice_dict = _apply_terminal_or_scalar(prev, event)
                    slice_dict = _attach_access_verification(slice_dict, event)
                    setattr(row, event.service.value, slice_dict)

                    # Reassign (not .append) so SQLAlchemy detects the JSON column change.
                    row.timeline = [
                        *(row.timeline or []),
                        event.model_dump(mode="json"),
                    ]
                    row.updated_at = _now()
                    session.flush()
                    return row.to_model(), True
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def upsert_snapshot(
        self,
        correlation_key: str,
        service: Service,
        state: ServiceState,
        *,
        title: str | None = None,
    ) -> WorkItem:
        """Update a service slice from a polled snapshot (no timeline entry).

        Used by the REST adapters (#7-#9), which reflect current upstream state
        rather than discrete events.
        """
        # Same INSERT race as upsert_from_event — retry once on collision.
        for attempt in range(2):
            try:
                with self._session.begin() as session:
                    row = self._get_row(session, correlation_key)
                    new_slice = state.model_dump()
                    existing = (getattr(row, service.value) if row is not None else None) or {}
                    # A poll that re-reports the SAME status/phase must NOT reset
                    # the liveness clock (#105). updated_at (onupdate=_now) drives
                    # the stall age, so re-stamping it on every no-op poll makes a
                    # hung stage (e.g. gen_functional_initial_started for hours)
                    # look perpetually fresh and never trip `stalled` — the
                    # cockpit then shows a dead task as "running" indefinitely.
                    # Skip the write entirely when nothing material changed so
                    # updated_at keeps the timestamp of the last REAL transition.
                    title_needed = bool(title) and not (row.title if row is not None else None)
                    unchanged = (
                        row is not None
                        and not title_needed
                        and existing.get("status") == new_slice.get("status")
                        and existing.get("phase") == new_slice.get("phase")
                        and (existing.get("usage") or None) == (new_slice.get("usage") or None)
                    )
                    if unchanged and row is not None:
                        return row.to_model()
                    if row is None:
                        row = WorkItemRow(
                            correlation_key=correlation_key,
                            timeline=[],
                            tenant_id=self._tenant or DEFAULT_TENANT,
                        )
                        session.add(row)
                    setattr(row, service.value, new_slice)
                    if title and not row.title:
                        row.title = title
                    row.updated_at = _now()
                    session.flush()
                    return row.to_model()
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def reconcile_snapshot(self, service: Service, live_task_ids: set[str]) -> int:
        """Clear stale non-terminal stages for ``service`` after a successful poll.

        When an upstream no longer reports a task that the store still holds as
        *non-terminal* (running/queued/in-review), its agent is gone — keeping the
        old "in_progress" makes the cockpit show ghosts. We blank that stage so the
        work item reflects reality (and drops off if it becomes empty). Terminal
        stages (done/failed) are preserved — completed history must persist, and
        completion-event-sourced items aren't in the poll list. Returns the number
        of stages cleared. Call ONLY after a successful fetch (never on error, or a
        transient outage would wipe live state)."""
        cleared = 0
        with self._session.begin() as session:
            rows = session.scalars(self._select()).all()
            for row in rows:
                data = getattr(row, service.value) or {}
                status = data.get("status")
                task_id = data.get("task_id")
                if not status or _is_terminal(status):
                    continue  # idle, or completed/failed — leave it
                if task_id is not None and task_id not in live_task_ids:
                    setattr(row, service.value, ServiceState().model_dump())
                    row.updated_at = _now()
                    cleared += 1
        return cleared

    def prune_duplicate_stages(self, service: Service, task_id_to_key: dict[str, str]) -> int:
        """Drop a service's stage from a work item when that task is now reported
        under a DIFFERENT correlation_key — i.e. the upstream re-keyed it.

        An early poll can key a task by a fallback id (e.g. ``af-<spec>``) before
        its real GitHub-issue key is exposed; once the issue appears the task is
        re-keyed, leaving an orphaned duplicate card frozen at its last (often
        terminal, e.g. ``failed``) stage that ``reconcile_snapshot`` deliberately
        preserves. This removes that orphan: clear the stale stage, and delete the
        row entirely if no stage data remains. Returns rows affected. Call only
        after a successful poll (``task_id_to_key`` from the same fetch)."""
        affected = 0
        with self._session.begin() as session:
            rows = session.scalars(self._select()).all()
            for row in rows:
                data = getattr(row, service.value) or {}
                tid = data.get("task_id")
                if tid is None:
                    continue
                canon = task_id_to_key.get(tid)
                if canon is not None and canon != row.correlation_key:
                    setattr(row, service.value, ServiceState().model_dump())
                    row.updated_at = _now()
                    affected += 1
                    if not _row_has_stage_data(row):
                        session.delete(row)
        return affected

    def prune_stuck(self, service: Service, stuck_task_ids: set[str]) -> int:
        """Delete work items whose ``service`` stage the upstream reports as
        stuck/stalled — its own liveness watchdog has given up (TFactory's #95
        sweep marks a hung stage ``stalled``).

        Auto-removes a dead task from the board instead of the cockpit showing it
        as "running" forever (which just confuses users). The whole row is dropped
        (not merely the stage): a task whose current stage is stuck is dead, and
        keeping its earlier ``done`` stages would leave a card that reads as
        finished. Robust against re-hydration — the caller also excludes these
        task ids from the hydrate/reconcile live set every poll, so a still-
        reported stalled task stays gone. Returns rows deleted. Call ONLY after a
        successful poll (``stuck_task_ids`` from the same fetch)."""
        if not stuck_task_ids:
            return 0
        deleted = 0
        with self._session.begin() as session:
            rows = session.scalars(self._select()).all()
            for row in rows:
                data = getattr(row, service.value) or {}
                tid = data.get("task_id")
                if tid is not None and tid in stuck_task_ids:
                    session.delete(row)
                    deleted += 1
        return deleted

    def get(self, correlation_key: str) -> WorkItem | None:
        with self._session() as session:
            row = self._get_row(session, correlation_key)
            return row.to_model() if row else None

    def list(self) -> list[WorkItem]:
        with self._session() as session:
            rows = session.scalars(self._select().order_by(WorkItemRow.updated_at.desc()))
            return [r.to_model() for r in rows]

    def delete(self, correlation_key: str) -> bool:
        """Remove a work item entirely from the cockpit store.

        Returns True if a row was deleted, False if none matched. Backs the HITL
        ``delete_task`` ("Remove") action so a finished/failed task can be cleared
        from the board. The upstream-factory delete is best-effort and separate:
        an orphaned card whose upstream task is already gone (e.g. a planner that
        failed before the task ever materialized) must still be removable here.
        """
        with self._session.begin() as session:
            row = self._get_row(session, correlation_key)
            if row is None:
                return False
            session.delete(row)
            return True

    def _get_row(self, session: Session, correlation_key: str) -> WorkItemRow | None:
        return session.scalars(
            self._select().where(WorkItemRow.correlation_key == correlation_key)
        ).first()


_store: WorkItemStore | None = None


def get_store(settings: Settings | None = None) -> WorkItemStore:
    """Cached default store, built from settings (lazy)."""
    global _store
    if _store is None:
        settings = settings or get_settings()
        _store = WorkItemStore(settings.database_url)
    return _store


def reset_store() -> None:
    """Drop the cached store (tests)."""
    global _store
    _store = None

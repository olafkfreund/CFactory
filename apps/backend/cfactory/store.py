"""Persistence for WorkItems — the cross-service correlation store.

A WorkItem is keyed by its correlation key (the GitHub issue number) and holds a
per-service slice (plan / code / test) plus an ordered event timeline. Completion
events upsert the matching slice and append to the timeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from .config import Settings, get_settings
from .db import Base, make_engine
from .models import CompletionEvent, Service, ServiceState, WorkItem


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _already_recorded(timeline: list | None, event: CompletionEvent) -> bool:
    """True if this (service, status) is already in the timeline — the RFC-0001
    idempotency key ``(service, correlation_key, status)`` (the key is the row)."""
    return any(
        e.get("service") == event.service.value and e.get("status") == event.status
        for e in (timeline or [])
    )


class WorkItemRow(Base):
    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
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
        )


class WorkItemStore:
    """Thin repository over the work_items table.

    Pass an explicit ``url`` for tests (a temp SQLite file); production resolves
    from settings (PostgreSQL). Tables are created on init for dev/test; real
    deployments manage schema via Alembic.
    """

    def __init__(self, url: str | None = None, *, create: bool = True) -> None:
        self._engine = make_engine(url)
        self._session = sessionmaker(self._engine, expire_on_commit=False)
        if create:
            Base.metadata.create_all(self._engine)

    def upsert_from_event(self, event: CompletionEvent) -> tuple[WorkItem, bool]:
        """Thread a completion event into its WorkItem.

        Idempotent by ``(service, correlation_key, status)`` per RFC-0001 §7: a
        duplicate (the same service already recorded at the same status for this
        key) is a no-op — the existing item is returned with ``applied=False`` and
        the timeline is left untouched, so retried/duplicated deliveries don't
        double-count. Returns ``(work_item, applied)``.
        """
        with self._session.begin() as session:
            row = self._get_row(session, event.correlation_key)
            if row is not None and _already_recorded(row.timeline, event):
                return row.to_model(), False

            if row is None:
                row = WorkItemRow(correlation_key=event.correlation_key, timeline=[])
                session.add(row)

            slice_ = ServiceState(
                task_id=event.task_id, status=event.status, phase=event.phase, usage=event.usage
            )
            setattr(row, event.service.value, slice_.model_dump())

            # Reassign (not .append) so SQLAlchemy detects the JSON column change.
            row.timeline = [*(row.timeline or []), event.model_dump(mode="json")]
            row.updated_at = _now()
            session.flush()
            return row.to_model(), True

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
        with self._session.begin() as session:
            row = self._get_row(session, correlation_key)
            if row is None:
                row = WorkItemRow(correlation_key=correlation_key, timeline=[])
                session.add(row)
            setattr(row, service.value, state.model_dump())
            if title and not row.title:
                row.title = title
            row.updated_at = _now()
            session.flush()
            return row.to_model()

    def get(self, correlation_key: str) -> WorkItem | None:
        with self._session() as session:
            row = self._get_row(session, correlation_key)
            return row.to_model() if row else None

    def list(self) -> list[WorkItem]:
        with self._session() as session:
            rows = session.scalars(select(WorkItemRow).order_by(WorkItemRow.updated_at.desc()))
            return [r.to_model() for r in rows]

    @staticmethod
    def _get_row(session: Session, correlation_key: str) -> WorkItemRow | None:
        return session.scalars(
            select(WorkItemRow).where(WorkItemRow.correlation_key == correlation_key)
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

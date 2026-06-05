"""Audit log for confirmed actions — the human-in-the-loop record.

Every CONFIRMED action that the cockpit executes against an upstream service is
recorded here: who/what/when, the target, and the outcome. This is the audit
trail for the "advise + confirm" principle — nothing reaches a service without a
human confirm, and every such confirm leaves a durable entry.

Mirrors :mod:`cfactory.store`: an ORM row on the shared ``Base`` plus a thin
repository with an injectable ``url`` (a temp SQLite file for tests, PostgreSQL
in real deployments).
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import Boolean, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .config import Settings, get_settings
from .db import Base, make_engine


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuditEntry(Base):
    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))
    correlation_key: Mapped[str] = mapped_column(String(128), index=True)
    target_service: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(512))
    status_code: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)

    def to_model(self) -> AuditEntryModel:
        return AuditEntryModel(
            id=self.id,
            ts=self.ts,
            actor=self.actor,
            kind=self.kind,
            correlation_key=self.correlation_key,
            target_service=self.target_service,
            endpoint=self.endpoint,
            status_code=self.status_code,
            ok=self.ok,
        )


class AuditEntryModel(BaseModel):
    """Serialisable view of an :class:`AuditEntry` row."""

    id: int
    ts: datetime
    actor: str
    kind: str
    correlation_key: str
    target_service: str
    endpoint: str
    status_code: int
    ok: bool


class AuditStore:
    """Thin repository over the audit_entries table.

    Pass an explicit ``url`` for tests (a temp SQLite file); production resolves
    from settings. Tables are created on init for dev/test; real deployments
    manage schema via Alembic.
    """

    def __init__(self, url: str | None = None, *, create: bool = True) -> None:
        self._engine = make_engine(url)
        self._session = sessionmaker(self._engine, expire_on_commit=False)
        if create:
            Base.metadata.create_all(self._engine)

    def record(
        self,
        *,
        actor: str,
        kind: str,
        correlation_key: str,
        target_service: str,
        endpoint: str,
        status_code: int,
        ok: bool,
    ) -> AuditEntryModel:
        """Append a new audit entry and return its serialisable model."""
        with self._session.begin() as session:
            row = AuditEntry(
                actor=actor,
                kind=kind,
                correlation_key=correlation_key,
                target_service=target_service,
                endpoint=endpoint,
                status_code=status_code,
                ok=ok,
            )
            session.add(row)
            session.flush()
            return row.to_model()

    def list(self, limit: int = 100) -> list[AuditEntryModel]:
        """Return the most recent entries, newest first."""
        with self._session() as session:
            rows = session.scalars(
                select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
            )
            return [r.to_model() for r in rows]


_audit_store: AuditStore | None = None


def get_audit_store(settings: Settings | None = None) -> AuditStore:
    """Cached default audit store, built from settings (lazy)."""
    global _audit_store
    if _audit_store is None:
        settings = settings or get_settings()
        _audit_store = AuditStore(settings.database_url)
    return _audit_store


def reset_audit_store() -> None:
    """Drop the cached audit store (tests)."""
    global _audit_store
    _audit_store = None

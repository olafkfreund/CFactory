"""Audit log for confirmed actions — the human-in-the-loop record.

Every CONFIRMED action that the cockpit executes against an upstream service is
recorded here: who/what/when, the target, and the outcome. This is the audit
trail for the "advise + confirm" principle — nothing reaches a service without a
human confirm, and every such confirm leaves a durable entry.

Tamper-evidence (#21): entries form an HMAC-anchored hash chain. Each row stores
``prev_hash`` (the previous entry's ``entry_hash``, ``None`` at genesis) and
``entry_hash = HMAC_SHA256(secret, canonical(fields) + prev_hash)``. Recomputing
the chain (:meth:`AuditStore.verify`) detects any after-the-fact mutation,
reordering, or deletion of an entry — the same anchoring AIFactory uses for its
enterprise audit trail, kept deliberately small here.

Mirrors :mod:`cfactory.store`: an ORM row on the shared ``Base`` plus a thin
repository with an injectable ``url`` (a temp SQLite file for tests, PostgreSQL
in real deployments).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import Boolean, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .config import Settings, get_settings
from .db import Base, make_engine


def _now() -> datetime:
    return datetime.now(UTC)


# Field order is part of the on-disk contract: the canonical string fed to the
# HMAC is built from these fields in this exact order. Changing it would
# invalidate every previously computed hash, so treat it as append-only.
_CANONICAL_FIELDS = (
    "ts",
    "actor",
    "kind",
    "correlation_key",
    "target_service",
    "endpoint",
    "status_code",
    "ok",
)


def _canonical_ts(value: datetime) -> str:
    """Render a timestamp to a stable UTC string.

    SQLite stores naive datetimes and returns them without tzinfo, so a
    tz-aware value written by ``record`` reads back naive. We normalise to UTC
    and emit a fixed microsecond format so the canonical form is identical
    whether the value came from memory or a round-trip through the DB.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _canonical(values: dict[str, object]) -> str:
    """Render the chained fields into a stable, unambiguous string.

    Uses ``\x1f`` (unit separator) between fields so field values can never
    collide with the delimiter, and a normalised UTC timestamp for ``ts``.
    """
    parts: list[str] = []
    for name in _CANONICAL_FIELDS:
        value = values[name]
        if isinstance(value, datetime):
            value = _canonical_ts(value)
        elif isinstance(value, bool):
            value = "1" if value else "0"
        parts.append(str(value))
    return "\x1f".join(parts)


def compute_entry_hash(secret: str, values: dict[str, object], prev_hash: str | None) -> str:
    """HMAC-SHA256 over the canonical fields chained to ``prev_hash``.

    ``prev_hash`` is the previous entry's ``entry_hash`` (or ``None`` at
    genesis). The genesis link folds in an empty string so the first entry is
    still bound to the secret.
    """
    message = f"{_canonical(values)}\x1f{prev_hash or ''}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


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
    # Tamper-evidence chain (#21). prev_hash is None for the genesis entry.
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64))

    def _hashed_values(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in _CANONICAL_FIELDS}

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
            prev_hash=self.prev_hash,
            entry_hash=self.entry_hash,
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
    prev_hash: str | None
    entry_hash: str


class AuditStore:
    """Thin repository over the audit_entries table.

    Pass an explicit ``url`` for tests (a temp SQLite file); production resolves
    from settings. Tables are created on init for dev/test; real deployments
    manage schema via Alembic.

    The HMAC secret anchoring the chain is taken from ``hmac_secret`` (defaults
    to the configured ``audit_hmac_secret`` setting).
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        create: bool = True,
        hmac_secret: str | None = None,
    ) -> None:
        self._engine = make_engine(url)
        self._session = sessionmaker(self._engine, expire_on_commit=False)
        self._secret = hmac_secret if hmac_secret is not None else get_settings().audit_hmac_secret
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
        """Append a new audit entry, chaining its hash to the previous entry."""
        with self._session.begin() as session:
            prev_hash = session.scalars(
                select(AuditEntry.entry_hash).order_by(AuditEntry.id.desc()).limit(1)
            ).first()
            row = AuditEntry(
                ts=_now(),
                actor=actor,
                kind=kind,
                correlation_key=correlation_key,
                target_service=target_service,
                endpoint=endpoint,
                status_code=status_code,
                ok=ok,
                prev_hash=prev_hash,
            )
            row.entry_hash = compute_entry_hash(self._secret, row._hashed_values(), prev_hash)
            session.add(row)
            session.flush()
            return row.to_model()

    def list(self, limit: int = 100) -> list[AuditEntryModel]:
        """Return the most recent entries, newest first."""
        with self._session() as session:
            rows = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit))
            return [r.to_model() for r in rows]

    def verify(self) -> list[int]:
        """Recompute the chain and return the ids of any tampered/broken entries.

        An empty list means the chain is intact. A non-empty list flags each
        entry whose stored ``entry_hash`` no longer matches the HMAC of its
        fields, or whose ``prev_hash`` does not link to the preceding entry
        (mutation, reordering, or deletion).
        """
        breaks: list[int] = []
        expected_prev: str | None = None
        with self._session() as session:
            rows = session.scalars(select(AuditEntry).order_by(AuditEntry.id.asc()))
            for row in rows:
                recomputed = compute_entry_hash(self._secret, row._hashed_values(), row.prev_hash)
                if row.prev_hash != expected_prev or row.entry_hash != recomputed:
                    breaks.append(row.id)
                expected_prev = row.entry_hash
        return breaks

    def is_intact(self) -> bool:
        """True when the chain verifies with no breaks."""
        return not self.verify()


_audit_store: AuditStore | None = None


def get_audit_store(settings: Settings | None = None) -> AuditStore:
    """Cached default audit store, built from settings (lazy)."""
    global _audit_store
    if _audit_store is None:
        settings = settings or get_settings()
        _audit_store = AuditStore(settings.database_url, hmac_secret=settings.audit_hmac_secret)
    return _audit_store


def reset_audit_store() -> None:
    """Drop the cached audit store (tests)."""
    global _audit_store
    _audit_store = None

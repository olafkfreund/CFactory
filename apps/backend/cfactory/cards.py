"""Planning cards — the human-owned half of the control plane (RFC-0019 §3.1).

A card is a planning-time intent: title, acceptance criteria, status, priority,
difficulty tier, assignee (a human handle *or* a factory runtime), milestone. It
carries a nullable ``correlation_key``, NULL while the card is only planned and
set when it enters the factory — that is the join back to ``work_items`` and the
RFC-0001 correlation timeline.

Cards live in their OWN table, deliberately. ``store.py``'s
``reconcile_snapshot``, ``prune_duplicate_stages``, ``prune_stuck`` and
``prune_stalled`` exist to blank stages and DELETE work-item rows whenever
upstream polling says the task is gone. That machinery is correct for a mirror of
upstream state and fatal for human-authored planning data, so none of it may be
able to reach a card. The RFC-0019 spike (§4, "design corrections") recommends
exactly this separation instead of a precedence rule.

Mirrors :mod:`cfactory.audit`: an ORM row on the shared ``Base`` plus a thin
repository with an injectable ``url`` (a temp SQLite file for tests, PostgreSQL
in real deployments).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Index, Integer, Select, String, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from .config import DEFAULT_TENANT, Settings, get_settings
from .db import Base, make_engine

# The board columns. A card moves between these; there is no other status space.
CardStatus = Literal["backlog", "ready", "in_progress", "blocked", "done"]
# RFC-0011 difficulty tiers, reused verbatim so a card can be dispatched to the
# intake path that already understands them.
CardTier = Literal["low", "medium", "hard"]

# Auto-assigned key prefix when the caller doesn't supply one (see ``create``).
_KEY_PREFIX = "FCT-"


def _now() -> datetime:
    return datetime.now(UTC)


class CardRow(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Stable human id ("FCT-42"). Unique PER TENANT, not globally — unlike
    # work_items.correlation_key, two tenants may legitimately both run an FCT-1.
    card_key: Mapped[str] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT, index=True
    )
    title: Mapped[str] = mapped_column(String(512))
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="backlog", server_default="backlog")
    # Lower = higher priority, so the backlog sorts ascending and a reprioritise
    # is a plain PATCH of this integer.
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    milestone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # NULL while the card is only planned; set ONCE when the card's work enters
    # the factory, at which point it joins work_items.correlation_key. Every
    # later stage of the same card REUSES it — see ``stage_runs`` for why.
    correlation_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # ── Per-stage dispatch record (RFC-0020 §3.7, Phase 7) ───────────────────
    # ``{"plan": {"service", "status", "dispatched_at", "ref", "detail"}, ...}``
    # keyed by stage name, where ``status`` is queued | dispatched | done |
    # failed. THIS is the idempotency guard for a stage action, and it has to be:
    # ``correlation_key`` cannot be, because planning SETS it, so the old
    # "non-NULL means already in the factory" rule made every stage after the
    # first a no-op and a plan -> code -> test sequence impossible. The key now
    # means "the key this card's work is threaded on — reuse it", and the
    # per-stage record answers "has THIS stage already run".
    stage_runs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # ── GitHub mirror (RFC-0019 §3.5, Phase 6) ───────────────────────────────
    # The issue this card is the planning projection of, as "owner/repo#123".
    # NULL means the card has no issue yet; non-NULL is also the idempotency
    # guard — exactly as correlation_key means "already in the factory", this
    # means "already has an issue", so a second sync adopts instead of creating.
    issue_ref: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    # Mirrored FROM GitHub, never pushed to it: GitHub is the record of truth for
    # these (see cfactory.github_sync — GitHub wins on conflict).
    issue_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Last sync failure, NULL when the last sync succeeded. A GitHub outage marks
    # the card here rather than raising: the board stays up and stays truthful
    # about the fact that it may now be stale.
    github_sync_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (Index("ix_cards_tenant_id_card_key", "tenant_id", "card_key", unique=True),)


class Card(BaseModel):
    """Serialisable view of a :class:`CardRow`."""

    model_config = ConfigDict(from_attributes=True)

    card_key: str
    tenant_id: str
    title: str
    acceptance_criteria: list[str]
    status: CardStatus
    priority: int
    tier: CardTier | None
    assignee: str | None
    milestone: str | None
    correlation_key: str | None
    issue_ref: str | None
    issue_state: str | None
    labels: list[str]
    github_sync_error: str | None
    # Read-only on the wire, like the mirrored GitHub columns: it records what
    # the factory was actually asked to do, so a caller must not be able to
    # assert a dispatch that never happened. Absent from CardCreate/CardUpdate.
    stage_runs: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CardCreate(BaseModel):
    """POST body. ``card_key`` is optional — omit it and the store assigns the
    next ``FCT-<n>`` for the tenant."""

    card_key: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: CardStatus = "backlog"
    priority: int = 0
    tier: CardTier | None = None
    assignee: str | None = Field(default=None, max_length=128)
    milestone: str | None = Field(default=None, max_length=128)
    correlation_key: str | None = Field(default=None, max_length=128)
    # Adopt an EXISTING issue ("owner/repo#123") instead of opening a new one
    # (RFC-0019 §3.5). The other three GitHub columns are deliberately absent
    # from both write models: they are mirrored FROM GitHub, so letting a caller
    # set them would let the board assert something GitHub never said.
    issue_ref: str | None = Field(default=None, max_length=256)


class CardUpdate(BaseModel):
    """PATCH body: every mutable field, all optional.

    Only the fields actually present in the request are applied (the route uses
    ``exclude_unset``), so ``{"status": "done"}`` is a move and
    ``{"priority": 3}`` is a reprioritise. ``card_key`` is immutable — it is the
    stable human id other systems quote.
    """

    title: str | None = Field(default=None, min_length=1, max_length=512)
    acceptance_criteria: list[str] | None = None
    status: CardStatus | None = None
    priority: int | None = None
    tier: CardTier | None = None
    assignee: str | None = Field(default=None, max_length=128)
    milestone: str | None = Field(default=None, max_length=128)
    correlation_key: str | None = Field(default=None, max_length=128)
    # Adopt (or re-point) the card's GitHub issue. See CardCreate for why the
    # mirrored columns are not writable here.
    issue_ref: str | None = Field(default=None, max_length=256)


class DuplicateCardKeyError(Exception):
    """Raised by :meth:`CardStore.create` when the tenant already has that key."""


# Columns added to ``cards`` after Phase 1, and the DDL that adds each to a live
# table: the Phase 6 GitHub mirror plus the Phase 7 per-stage dispatch record.
# Every one is nullable or defaulted, so backfilling an existing board is a
# no-op: a pre-Phase-6 card simply has no issue, a pre-Phase-7 one no stage runs.
_LATE_COLUMNS = {
    "issue_ref": "VARCHAR(256)",
    "issue_state": "VARCHAR(16)",
    "labels": "JSON NOT NULL DEFAULT '[]'",
    "github_sync_error": "VARCHAR(512)",
    "stage_runs": "JSON NOT NULL DEFAULT '{}'",
}


def _ensure_late_columns(engine: Engine) -> None:
    """Idempotent live-DB guard for the post-Phase-1 card columns.

    Same reason as ``store._ensure_tenant_column``: the deployed store
    bootstraps via ``create_all``, which never ALTERs an existing table, so a
    cards table created in Phase 1 would 500 every SELECT once the model gained
    these columns. The Alembic migrations exist too; this covers deploys that
    don't run them.
    """
    from sqlalchemy import inspect, text  # noqa: PLC0415 — init-time only

    inspector = inspect(engine)
    if not inspector.has_table(CardRow.__tablename__):
        return
    existing = {c["name"] for c in inspector.get_columns(CardRow.__tablename__)}
    missing = {name: ddl for name, ddl in _LATE_COLUMNS.items() if name not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(text(f"ALTER TABLE cards ADD COLUMN {name} {ddl}"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_cards_issue_ref ON cards (issue_ref)"))


class CardStore:
    """Thin repository over the cards table.

    Tenant scoping mirrors :class:`~cfactory.store.WorkItemStore` exactly: an
    unscoped store (the default, single-tenant mode) sees every row;
    ``scoped(tenant)`` returns a view whose reads filter by ``tenant_id`` and
    whose writes stamp it. ``cards_store_dep`` hands routes a scoped view only
    when CFACTORY_MULTI_TENANT is on.

    ponytail: the scoping trio (``_tenant``/``scoped``/``_select``) is copied
    from WorkItemStore rather than hoisted into a shared base — two stores don't
    justify refactoring the heavily-tested work-item path. Extract it if a third
    appears.
    """

    def __init__(self, url: str | None = None, *, create: bool = True) -> None:
        self._engine = make_engine(url)
        self._session = sessionmaker(self._engine, expire_on_commit=False)
        self._tenant: str | None = None  # None = unscoped (single-tenant mode)
        if create:
            Base.metadata.create_all(self._engine)
            _ensure_late_columns(self._engine)

    def scoped(self, tenant: str) -> CardStore:
        """A tenant-scoped view sharing this store's engine/session factory."""
        view = copy.copy(self)
        view._tenant = tenant
        return view

    def _select(self) -> Select[tuple[CardRow]]:
        """Base SELECT honouring the tenant scope (no filter when unscoped)."""
        stmt = select(CardRow)
        if self._tenant is not None:
            stmt = stmt.where(CardRow.tenant_id == self._tenant)
        return stmt

    def _get_row(self, session: Session, card_key: str) -> CardRow | None:
        return session.scalars(self._select().where(CardRow.card_key == card_key)).first()

    def list(
        self,
        *,
        status: str | None = None,
        milestone: str | None = None,
        assignee: str | None = None,
        tier: str | None = None,
    ) -> list[Card]:
        """Cards for this tenant, highest priority first, then oldest first."""
        stmt = self._select()
        for column, value in (
            (CardRow.status, status),
            (CardRow.milestone, milestone),
            (CardRow.assignee, assignee),
            (CardRow.tier, tier),
        ):
            if value is not None:
                stmt = stmt.where(column == value)
        stmt = stmt.order_by(CardRow.priority.asc(), CardRow.created_at.asc())
        with self._session() as session:
            return [Card.model_validate(row) for row in session.scalars(stmt)]

    def get(self, card_key: str) -> Card | None:
        with self._session() as session:
            row = self._get_row(session, card_key)
            return Card.model_validate(row) if row is not None else None

    def get_by_correlation_key(self, correlation_key: str) -> Card | None:
        """The card joined to a work item, if any (RFC-0019 §3.2 write-back).

        The reverse of :attr:`CardRow.correlation_key`'s forward join, used by
        the event ingress to find which card a PARR event belongs to. Returns
        the oldest match — the column is not unique (nothing stops two cards
        being pointed at one correlation), so this is deliberately first-wins
        rather than an error.
        """
        stmt = self._select().where(CardRow.correlation_key == correlation_key)
        stmt = stmt.order_by(CardRow.created_at.asc())
        with self._session() as session:
            row = session.scalars(stmt).first()
            return Card.model_validate(row) if row is not None else None

    def create(self, data: CardCreate) -> Card:
        """Insert a card, assigning a key when the caller omitted one.

        Raises :class:`DuplicateCardKeyError` if the tenant already holds that key —
        which also covers the (rare) race where two concurrent auto-assigns pick
        the same ``FCT-<n>``; the loser retries by asking again.
        """
        fields = data.model_dump()
        tenant = self._tenant or DEFAULT_TENANT
        try:
            with self._session.begin() as session:
                if not fields.get("card_key"):
                    fields["card_key"] = self._next_card_key(session)
                row = CardRow(**fields, tenant_id=tenant)
                session.add(row)
                session.flush()
                return Card.model_validate(row)
        except IntegrityError as exc:
            raise DuplicateCardKeyError(fields["card_key"]) from exc

    def _next_card_key(self, session: Session) -> str:
        """Next ``FCT-<n>`` for this tenant: highest existing suffix + 1.

        ponytail: scans the tenant's keys rather than keeping a counter table —
        a planning board is hundreds of rows, not millions. Swap in a sequence
        if a tenant's backlog ever gets big enough to notice.
        """
        highest = 0
        for (key,) in session.execute(self._select().with_only_columns(CardRow.card_key)):
            suffix = key.removeprefix(_KEY_PREFIX)
            if key.startswith(_KEY_PREFIX) and suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{_KEY_PREFIX}{highest + 1}"

    def update(self, card_key: str, changes: dict[str, object]) -> Card | None:
        """Apply a partial update. Returns None when no card matches (in scope).

        ``changes`` is the PATCH body with unset fields excluded, so an absent
        field is left alone and an explicit ``null`` clears it.
        """
        with self._session.begin() as session:
            row = self._get_row(session, card_key)
            if row is None:
                return None
            for field, value in changes.items():
                setattr(row, field, value)
            row.updated_at = _now()
            session.flush()
            return Card.model_validate(row)

    def delete(self, card_key: str) -> bool:
        """Remove a card. Returns True if a row was deleted, False if none matched."""
        with self._session.begin() as session:
            row = self._get_row(session, card_key)
            if row is None:
                return False
            session.delete(row)
            return True


_cards_store: CardStore | None = None


def get_cards_store(settings: Settings | None = None) -> CardStore:
    """Cached default card store, built from settings (lazy)."""
    global _cards_store  # noqa: PLW0603 — cached singleton, as get_store/get_audit_store
    if _cards_store is None:
        settings = settings or get_settings()
        _cards_store = CardStore(settings.database_url)
    return _cards_store


def reset_cards_store() -> None:
    """Drop the cached card store (tests)."""
    global _cards_store  # noqa: PLW0603 — see get_cards_store
    _cards_store = None

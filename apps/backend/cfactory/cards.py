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
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Index, Integer, Select, String, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from .audit import AuditStore
from .config import DEFAULT_TENANT, Settings, get_settings
from .credentials import (
    Credential,
    CredentialError,
    CredentialInfo,
    GitCredentialRow,
    env_credential,
    load_keyring,
    require_keyring,
    rewrap,
    seal,
    unseal,
)
from .db import Base, make_engine
from .git_config import (
    GitConfig,
    GitConfigRow,
    GitConfigUpdate,
    GitTarget,
    config_view,
    provider_token,
    target_from_row,
    target_from_settings,
    validated_fields,
)

logger = logging.getLogger(__name__)

# The board columns. A card moves between these; there is no other status space.
CardStatus = Literal["backlog", "ready", "in_progress", "blocked", "done"]
# RFC-0011 difficulty tiers, reused verbatim so a card can be dispatched to the
# intake path that already understands them.
CardTier = Literal["low", "medium", "hard"]

# Auto-assigned key prefix when the caller doesn't supply one (see ``create``).
_KEY_PREFIX = "FCT-"

# The actor recorded when a credential is read by a path with no human behind it
# — the card-write sync hook and the background import. Honest rather than
# flattering: the mutation that TRIGGERED the read is audited separately, with
# its real actor, immediately beside this entry in the same chain.
SYSTEM_ACTOR = "system"

# Audit ``target_service`` for a credential access. The same value the git-config
# mutations use — the thing being reached is the git provider.
_CREDENTIAL_TARGET = "git_provider"


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """A stored timestamp as an aware UTC one.

    SQLite hands back naive datetimes from a ``DateTime`` column even when an
    aware one went in, and the watermark is compared against provider timestamps
    that are always aware — so a naive read would raise on the comparison.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


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
    # Free-form markdown body (RFC-0020 §3.6). Where an imported issue's body
    # lands — and a MIRRORED field, so the host owns it exactly as it owns the
    # title. Deliberately NOT acceptance_criteria: see cfactory.issue_import.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # Soft delete (RFC-0020 §3.6). Deleting a card means "not on my board", never
    # "destroy the record of truth" — the issue is untouched. The row stays for
    # two reasons: the unique index below keeps doing its job, and the next
    # import sees the tombstone and does NOT resurrect the card.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_cards_tenant_id_card_key", "tenant_id", "card_key", unique=True),
        # Import idempotency, enforced by the DATABASE rather than by an
        # application-level "does this exist?" check (RFC-0020 §3.6). The check
        # loses a race between two concurrent polls; the constraint does not.
        # NULLs are distinct in both SQLite and PostgreSQL, so the unconstrained
        # majority of cards — the ones with no issue — are unaffected.
        Index("ix_cards_tenant_id_issue_ref", "tenant_id", "issue_ref", unique=True),
    )


class Card(BaseModel):
    """Serialisable view of a :class:`CardRow`."""

    model_config = ConfigDict(from_attributes=True)

    card_key: str
    tenant_id: str
    title: str
    description: str | None = None
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
    # Always NULL on anything a read hands back — a soft-deleted card is off the
    # board. It is on the model because the import asks for a card BY ISSUE and
    # has to be able to see the tombstone (RFC-0020 §3.6).
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CardCreate(BaseModel):
    """POST body. ``card_key`` is optional — omit it and the store assigns the
    next ``FCT-<n>`` for the tenant."""

    card_key: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    # Free-form markdown (RFC-0020 §3.6). An imported issue's body lands here.
    description: str | None = None
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
    description: str | None = None
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


class ImportStateRow(Base):
    """The poll watermark for one (tenant, project) — RFC-0020 §3.6.

    One row per repository a tenant imports from, holding the ``since`` the next
    incremental pass asks the provider for. Kept beside the cards rather than in
    a service table because it is *about* this tenant's cards and shares their
    lifetime; there is no separate git-config table to hang it off yet.
    """

    __tablename__ = "card_import_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT
    )
    project: Mapped[str] = mapped_column(String(256))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_card_import_state_tenant_project", "tenant_id", "project", unique=True),
    )


class DuplicateCardKeyError(Exception):
    """Raised by :meth:`CardStore.create` when the tenant already has that key."""


class DuplicateIssueRefError(Exception):
    """Raised by :meth:`CardStore.create` when the tenant already has a card for
    that issue — the unique (tenant_id, issue_ref) index firing.

    Distinct from :class:`DuplicateCardKeyError` because the callers differ: a
    duplicate key is a 409 for the human who chose it, while a duplicate issue
    ref is the import's *normal* concurrent-poll outcome, handled by switching
    from insert to update.
    """


# Columns added to ``cards`` after Phase 1, and the DDL that adds each to a live
# table: the Phase 6 GitHub mirror, the §3.6 import columns, and the Phase 7
# per-stage dispatch record. Every one is nullable or defaulted, so backfilling
# an existing board is a no-op: a pre-Phase-6 card simply has no issue and no
# body, a pre-Phase-7 one no stage runs.
_LATE_COLUMNS = {
    "issue_ref": "VARCHAR(256)",
    "issue_state": "VARCHAR(16)",
    "labels": "JSON NOT NULL DEFAULT '[]'",
    "github_sync_error": "VARCHAR(512)",
    "stage_runs": "JSON NOT NULL DEFAULT '{}'",
    "description": "TEXT",
    # TIMESTAMP, not DATETIME: PostgreSQL has no DATETIME, and SQLite accepts
    # any type name.
    "deleted_at": "TIMESTAMP",
}

# The same guard for ``tenant_git_config``, which RFC-0020 phase 2 shipped and
# phase 3 gave one more column. A live board created by phase 2 already HAS the
# table, so ``create_all`` will not add it and every config read would fail.
_LATE_CONFIG_COLUMNS = {"credential_rejected": "BOOLEAN"}

# Indexes the late columns need. The unique one is the RFC-0020 §3.6 import
# idempotency guard and must exist on a live DB too, not only on a fresh
# create_all — without it two concurrent polls duplicate every card.
_LATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_cards_issue_ref ON cards (issue_ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_cards_tenant_id_issue_ref"
    " ON cards (tenant_id, issue_ref)",
)


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
    if inspector.has_table(GitConfigRow.__tablename__):
        present = {c["name"] for c in inspector.get_columns(GitConfigRow.__tablename__)}
        with engine.begin() as conn:
            for name, ddl in _LATE_CONFIG_COLUMNS.items():
                if name not in present:
                    conn.execute(
                        text(f"ALTER TABLE {GitConfigRow.__tablename__} ADD COLUMN {name} {ddl}")
                    )
    if not inspector.has_table(CardRow.__tablename__):
        return
    existing = {c["name"] for c in inspector.get_columns(CardRow.__tablename__)}
    missing = {name: ddl for name, ddl in _LATE_COLUMNS.items() if name not in existing}
    # No early return when nothing is missing: the indexes below still have to be
    # ensured on a board whose columns were added by an earlier release.
    with engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(text(f"ALTER TABLE cards ADD COLUMN {name} {ddl}"))
    for statement in _LATE_INDEXES:
        # Each in its own transaction: a board that already holds two cards
        # pointing at ONE issue (nothing forbade it before this index) cannot
        # take the unique one, and that must not abort the others — nor take the
        # board down at boot. It is logged loudly instead, because until the
        # duplicates are merged the import falls back to its application-level
        # check and a concurrent poll can duplicate a card.
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
        except Exception as exc:  # noqa: BLE001 — see above; boot must survive.
            logger.warning("could not create card index (%s): %s", statement.split()[-1], exc)


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
        self._url = url
        self._audit: AuditStore | None = None
        if create:
            Base.metadata.create_all(self._engine)
            _ensure_late_columns(self._engine)

    def scoped(self, tenant: str) -> CardStore:
        """A tenant-scoped view sharing this store's engine/session factory."""
        view = copy.copy(self)
        view._tenant = tenant
        return view

    @property
    def tenant(self) -> str:
        """The tenant this store reads and writes as.

        An unscoped store (single-tenant mode) is the ``default`` tenant, which
        is exactly the value its writes already stamp — so the git config a card
        write resolves is the one it would have been stamped with.
        """
        return self._tenant or DEFAULT_TENANT

    def _select(self, *, include_deleted: bool = False) -> Select[tuple[CardRow]]:
        """Base SELECT honouring the tenant scope (no filter when unscoped).

        Soft-deleted cards are invisible by default — a deleted card is off the
        board for every read. Two callers pass ``include_deleted``: the import,
        which must SEE the tombstone so it does not resurrect the card, and key
        assignment, which must not hand out a key a tombstone still holds.
        """
        stmt = select(CardRow)
        if self._tenant is not None:
            stmt = stmt.where(CardRow.tenant_id == self._tenant)
        if not include_deleted:
            stmt = stmt.where(CardRow.deleted_at.is_(None))
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

    def get_by_issue_ref(self, issue_ref: str) -> Card | None:
        """The card for a provider issue, **including a soft-deleted one**.

        The import's lookup half (RFC-0020 §3.6). A tombstone must come back
        here: it is the answer to "should this issue become a card?" and the
        answer is no, the human took it off the board.
        """
        stmt = self._select(include_deleted=True).where(CardRow.issue_ref == issue_ref)
        with self._session() as session:
            row = session.scalars(stmt).first()
            return Card.model_validate(row) if row is not None else None

    def create(self, data: CardCreate) -> Card:
        """Insert a card, assigning a key when the caller omitted one.

        Raises :class:`DuplicateCardKeyError` if the tenant already holds that key —
        which also covers the (rare) race where two concurrent auto-assigns pick
        the same ``FCT-<n>``; the loser retries by asking again. Raises
        :class:`DuplicateIssueRefError` when the tenant already holds a card for
        this issue, which is how a concurrent import learns to update instead.
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
            # Which constraint fired? Asked of the database rather than parsed
            # out of the driver's message, which differs per backend.
            ref = fields.get("issue_ref")
            if ref and self.get_by_issue_ref(str(ref)) is not None:
                raise DuplicateIssueRefError(ref) from exc
            raise DuplicateCardKeyError(fields["card_key"]) from exc

    def _next_card_key(self, session: Session) -> str:
        """Next ``FCT-<n>`` for this tenant: highest existing suffix + 1.

        Counts soft-deleted rows too: their keys are still taken (the
        (tenant, card_key) index does not care that a card is off the board), so
        skipping them would hand out a key that then fails to insert.

        ponytail: scans the tenant's keys rather than keeping a counter table —
        a planning board is hundreds of rows, not millions. Swap in a sequence
        if a tenant's backlog ever gets big enough to notice.
        """
        highest = 0
        keys = self._select(include_deleted=True).with_only_columns(CardRow.card_key)
        for (key,) in session.execute(keys):
            suffix = key.removeprefix(_KEY_PREFIX)
            if key.startswith(_KEY_PREFIX) and suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{_KEY_PREFIX}{highest + 1}"

    def update(self, card_key: str, changes: dict[str, object]) -> Card | None:
        """Apply a partial update. Returns None when no card matches (in scope).

        ``changes`` is the PATCH body with unset fields excluded, so an absent
        field is left alone and an explicit ``null`` clears it.

        Raises :class:`DuplicateIssueRefError` when the update would point a
        second card at an issue this tenant already has one for — the same
        unique index the import relies on, which a PATCH can now also hit.
        """
        try:
            with self._session.begin() as session:
                row = self._get_row(session, card_key)
                if row is None:
                    return None
                for field, value in changes.items():
                    setattr(row, field, value)
                row.updated_at = _now()
                session.flush()
                return Card.model_validate(row)
        except IntegrityError as exc:
            raise DuplicateIssueRefError(changes.get("issue_ref")) from exc

    def delete(self, card_key: str) -> bool:
        """Take a card off the board. True if one was deleted, False if none matched.

        A **soft** delete (RFC-0020 §3.6): the row is tombstoned, not removed.
        Every read hides it, so the board behaves exactly as before, but the
        issue it came from stays claimed — which is what stops the next import
        resurrecting a card the human deliberately removed, and what keeps the
        unique (tenant, issue_ref) index meaningful. The issue on the host is
        never touched: deleting a card means "not on my board", not "destroy the
        record of truth".
        """
        with self._session.begin() as session:
            row = self._get_row(session, card_key)
            if row is None:
                return False
            row.deleted_at = _now()
            return True

    # ── Tenant git configuration (RFC-0020 §3.3) ─────────────────────────────
    #
    # Hung off the card store rather than given a store of its own, for the same
    # reason ``ImportStateRow`` is: it is *about* this tenant's cards, shares
    # their lifetime, and — decisively — every consumer of the git config
    # (github_sync, issue_import, card_intake) is already handed a tenant-scoped
    # CardStore. A second store would mean a second tenant-scoping mechanism to
    # keep in step with this one, which is precisely how a cross-tenant read gets
    # written by accident.

    def git_config_row(self) -> GitConfigRow | None:
        """This tenant's stored git configuration row, or None if it has none."""
        stmt = select(GitConfigRow).where(GitConfigRow.tenant_id == self.tenant)
        with self._session() as session:
            return session.scalars(stmt).first()

    def git_target(
        self,
        settings: Settings | None = None,
        *,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> GitTarget:
        """This tenant's git target: the stored row if there is one, else the env.

        The ONE resolution every consumer uses — ``github_sync`` (which project
        an issue is opened in), ``issue_import`` (which project issues are read
        from) and ``card_intake`` (which AIFactory project a card is built in).
        None of them looks at ``Settings`` for a provider, a repo or an intake
        project any more; if they did, the stored configuration would be a second
        opinion rather than the answer.

        It hangs off the store because the store is what knows the tenant: every
        consumer is already handed a tenant-scoped one, so tenant-correct
        configuration needs no tenant id threaded through five call signatures.

        ``actor`` and ``audit`` are stamped onto the audit entry the credential
        writes IF it is fetched (RFC-0020 §3.4). Resolving a target does not read
        a credential — the panel asks for a target on every poll and must not
        decrypt anything to answer — so a target that is never handed to a
        provider produces no entry.
        """
        settings = settings or get_settings()
        credential = self.git_credential(settings, actor=actor, audit=audit)
        row = self.git_config_row()
        if row is None:
            return target_from_settings(settings, self.tenant, credential)
        return target_from_row(row, settings, credential)

    def seed_git_config_from_env(self, settings: Settings | None = None) -> GitConfig | None:
        """Materialise this tenant's config from the legacy env vars. Once.

        RFC-0020 §3.3: ``CFACTORY_INTAKE_PROJECT_ID`` (and, on the same rule, the
        ``CFACTORY_GITHUB_*`` / ``CFACTORY_GIT_PROVIDER_*`` project settings) are
        retired as globals but survive one release as a seed, so an existing
        single-tenant deployment keeps working with **no operator action** and
        its values become editable in the portal.

        Two rules make this safe to call on every boot:

        * a tenant that already has a row is left ALONE — the stored config is
          authoritative, and re-seeding would silently undo an edit made in the
          cockpit every time the process restarted (the failure this is most
          likely to cause, and the one the tests pin);
        * nothing to seed (no project, no AIFactory project id) writes no row, so
          a deploy that never configured any of this stays ``unconfigured``
          rather than acquiring an empty row that reads as a deliberate choice.

        Returns the seeded config, or ``None`` when it did nothing.
        """
        settings = settings or get_settings()
        if self.git_config_row() is not None:
            return None
        env = target_from_settings(settings, self.tenant)
        if not (env.project or env.aifactory_project_id):
            return None
        logger.info(
            "seeding tenant %r git config from the environment (RFC-0020 §3.3): "
            "provider=%s project=%s aifactory_project_id=%s",
            self.tenant,
            env.provider,
            env.project,
            "set" if env.aifactory_project_id else "unset",
        )
        self.set_git_config(
            GitConfigUpdate(
                provider=env.provider,
                base_url=env.base_url,
                project=env.project,
                aifactory_project_id=env.aifactory_project_id,
            ),
            settings,
        )
        return config_view(self.git_target(settings))

    def set_git_config(self, update: GitConfigUpdate, settings: Settings | None = None) -> None:
        """Replace this tenant's git configuration. Raises ``GitConfigError``.

        A full replacement (the PUT semantics), and it clears any recorded
        verification: that verification proved a configuration this one is no
        longer. Returns nothing — the caller re-resolves, so "what is the config
        now" has one implementation (``git_target``) rather than a
        second, subtly different one on the write path.
        """
        self._upsert_git_config(validated_fields(update), settings)

    # ── Tenant git credential (RFC-0020 §3.4) ────────────────────────────────
    #
    # Hung off the card store for exactly the reason the git configuration is
    # (see the comment above ``git_config_row``): the store is what knows the
    # tenant, and a credential is the one resource where a second, subtly
    # different tenant-scoping mechanism would be a cross-tenant read.
    #
    # The crypto itself is in :mod:`cfactory.credentials`. What lives here is the
    # storage, the tenant scope, and the audit entry every read appends.

    def audit_store(self) -> AuditStore:
        """The audit chain a credential read is appended to.

        The SAME chain (RFC-0001a) every card and config mutation uses, on this
        store's own database — cards, configurations, credentials and audit
        entries all share one ``Base``, so a credential read is chained into the
        same tamper-evident sequence as the action that needed it. Built lazily
        because most stores never read a credential.
        """
        if self._audit is None:
            self._audit = AuditStore(self._url)
        return self._audit

    def git_credential_row(self) -> GitCredentialRow | None:
        """This tenant's sealed credential row, or None if it has none."""
        stmt = select(GitCredentialRow).where(GitCredentialRow.tenant_id == self.tenant)
        with self._session() as session:
            return session.scalars(stmt).first()

    def git_credential(
        self,
        settings: Settings | None = None,
        *,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> Credential:
        """This tenant's credential handle — never the credential.

        A stored credential is the answer whether or not it can currently be
        unsealed; it does NOT fall back to the deployment's environment token.
        Falling back would hand tenant A the operator's credential the moment
        tenant A's own record became unreadable, which is the cross-tenant leak
        this whole phase exists to close. Only a tenant that has stored nothing
        uses the environment one.

        ``configured`` is answered WITHOUT decrypting: a row exists and this
        process holds the key that wraps it. A key of the right id but the wrong
        material therefore reads as configured and fails at fetch time, which the
        board reports as a rejected credential rather than as a green one — the
        alternative is decrypting a secret to render a boolean on every poll.
        """
        settings = settings or get_settings()
        row = self.git_credential_row()
        if row is None:
            return env_credential(provider_token(settings))
        return Credential(
            CredentialInfo(
                configured=self._holds_key(row.key_version, settings),
                source="tenant",
                updated_at=_as_utc(row.updated_at),
                key_version=row.key_version,
            ),
            lambda: self._fetch_git_credential(settings, actor=actor, audit=audit),
        )

    def _holds_key(self, key_version: str, settings: Settings) -> bool:
        """Whether this process holds the KEK that wraps *key_version*."""
        try:
            keyring = load_keyring(settings)
        except CredentialError as exc:
            logger.error("credential key is unusable for tenant %s: %s", self.tenant, exc)
            return False
        return keyring is not None and keyring.find(key_version) is not None

    def _fetch_git_credential(
        self, settings: Settings, *, actor: str, audit: AuditStore | None
    ) -> str | None:
        """Unseal this tenant's credential for ONE provider call, and audit it.

        Every outcome is chained, including the failures: "the credential could
        not be read at 14:02" is precisely the entry an operator needs when a
        board goes quiet after a key rotation, and an audit trail that only
        records the successes cannot answer that.

        Never raises. A missing key, an unusable key or an altered record yields
        no credential, which the board renders as ``credential_missing`` and
        keeps serving — a credential problem degrades the board, it does not take
        it down.
        """
        row = self.git_credential_row()
        if row is None:
            return None
        try:
            keyring = load_keyring(settings)
            if keyring is None:
                # Raised rather than returned so every failure leaves by one
                # path — logged, audited, and yielding no credential.
                raise CredentialError(
                    f"no credential key is configured, so the stored credential for "
                    f"tenant {self.tenant!r} cannot be read"
                )
            secret = unseal(row.sealed(), tenant=self.tenant, keyring=keyring)
        except CredentialError as exc:
            # The message names the tenant and the failure, never the record and
            # never a fragment of the credential.
            logger.error("credential read failed for tenant %s: %s", self.tenant, exc)
            self._audit_credential(audit, actor, kind="read_git_credential", ok=False)
            return None
        self._audit_credential(audit, actor, kind="read_git_credential", ok=True)
        self._rewrap_git_credential(row, keyring)
        return secret

    def _rewrap_git_credential(self, row: GitCredentialRow, keyring: Any) -> None:
        """Move a record onto the active KEK, if it is not already on it.

        The rotation story: put the new key FIRST in ``CFACTORY_CREDENTIAL_KEY``,
        keep the old one listed, and records migrate as they are used. The
        credential is not decrypted to do it — only its data key is re-wrapped
        (see :func:`cfactory.credentials.rewrap`).

        ponytail: lazy, on read. A tenant whose credential is never used never
        migrates, so check every tenant reports the new ``key_version`` in the
        panel before dropping the old key from the environment. Upgrade path if
        that ever bites: sweep every row at boot.
        """
        try:
            rewrapped = rewrap(row.sealed(), tenant=self.tenant, keyring=keyring)
            if rewrapped is not None:
                self._store_sealed(rewrapped)
                logger.info(
                    "re-wrapped tenant %s git credential onto key %s",
                    self.tenant,
                    rewrapped.key_version,
                )
        except CredentialError as exc:  # pragma: no cover — unwrapping just succeeded
            logger.warning("could not re-wrap tenant %s git credential: %s", self.tenant, exc)

    def set_git_credential(self, secret: str, settings: Settings | None = None) -> CredentialInfo:
        """Store (or replace) this tenant's credential, encrypted.

        FAILS CLOSED: with no ``CFACTORY_CREDENTIAL_KEY`` configured this raises
        :class:`~cfactory.credentials.CredentialError` rather than writing
        anything. There is no plaintext path, not even a degraded one.
        """
        settings = settings or get_settings()
        value = (secret or "").strip()
        if not value:
            raise CredentialError("credential must not be empty")
        sealed = seal(value, tenant=self.tenant, keyring=require_keyring(settings))
        self._store_sealed(sealed)
        # A new credential makes any recorded rejection obsolete — it was about
        # the credential this one replaces. Only touched when a configuration row
        # already exists: storing a credential must not materialise an empty
        # configuration that then reads as a deliberate choice.
        if self.git_config_row() is not None:
            self._upsert_git_config({"credential_rejected": None}, settings)
        return CredentialInfo(
            configured=True,
            source="tenant",
            updated_at=_now(),
            key_version=sealed.key_version,
        )

    def clear_git_credential(self) -> bool:
        """Forget this tenant's credential. True if there was one.

        The revocation path: a credential that has leaked has to be removable
        from the surface that stored it, not by an operator with a SQL client.
        """
        stmt = select(GitCredentialRow).where(GitCredentialRow.tenant_id == self.tenant)
        with self._session.begin() as session:
            row = session.scalars(stmt).first()
            if row is None:
                return False
            session.delete(row)
            return True

    def _store_sealed(self, sealed: Any) -> None:
        """Insert-or-update this tenant's sealed credential.

        Same constraint-not-check rule as ``_upsert_git_config``: two concurrent
        first-ever writes both find no row and both insert, and the unique index
        rejects the loser, which then takes the update path.
        """
        stmt = select(GitCredentialRow).where(GitCredentialRow.tenant_id == self.tenant)
        for attempt in range(2):
            try:
                with self._session.begin() as session:
                    row = session.scalars(stmt).first()
                    if row is None:
                        session.add(
                            GitCredentialRow(
                                tenant_id=self.tenant,
                                key_version=sealed.key_version,
                                wrapped_key=sealed.wrapped_key,
                                ciphertext=sealed.ciphertext,
                            )
                        )
                    else:
                        row.key_version = sealed.key_version
                        row.wrapped_key = sealed.wrapped_key
                        row.ciphertext = sealed.ciphertext
                        row.updated_at = _now()
                    session.flush()
                    return
            except IntegrityError:
                if attempt:  # pragma: no cover — the row exists by the retry
                    raise

    def _audit_credential(
        self, audit: AuditStore | None, actor: str, *, kind: str, ok: bool
    ) -> None:
        """Append a credential access to the shared tamper-evident chain."""
        (audit or self.audit_store()).record(
            actor=actor,
            kind=kind,
            correlation_key=f"tenant:{self.tenant}",
            target_service=_CREDENTIAL_TARGET,
            endpoint=f"/api/tenants/{self.tenant}/git-credential",
            status_code=200 if ok else 0,
            ok=ok,
        )

    def record_git_verification(
        self,
        *,
        error: str | None,
        rejected: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Record the outcome of a verify against this tenant's configuration.

        Materialises the row when the tenant is still resolving from the
        environment: asking to verify is asking about a *specific* configuration,
        and the answer has to be recorded against something. What is materialised
        is exactly what the seed would have written, so this cannot invent a
        configuration the deployment did not already describe.
        """
        if self.git_config_row() is None:
            env = target_from_settings(settings or get_settings(), self.tenant)
            self.set_git_config(
                GitConfigUpdate(
                    provider=env.provider,
                    base_url=env.base_url,
                    project=env.project,
                    aifactory_project_id=env.aifactory_project_id,
                ),
                settings,
            )
        self._upsert_git_config(
            {
                "verified_at": None if error else _now(),
                "verify_error": error,
                # A successful verify proves the credential was ACCEPTED, so it
                # clears any earlier rejection as well as recording the success.
                "credential_rejected": bool(rejected) if error else None,
            },
            settings,
        )

    def _upsert_git_config(self, fields: dict[str, Any], _settings: Settings | None = None) -> None:
        """Insert-or-update this tenant's config row.

        Two concurrent first-ever writes both find no row and both insert; the
        unique index rejects the loser, which then takes the update path — the
        same constraint-not-check rule the card import follows.
        """
        stmt = select(GitConfigRow).where(GitConfigRow.tenant_id == self.tenant)
        for attempt in range(2):
            try:
                with self._session.begin() as session:
                    row = session.scalars(stmt).first()
                    if row is None:
                        session.add(GitConfigRow(tenant_id=self.tenant, **fields))
                    else:
                        for name, value in fields.items():
                            setattr(row, name, value)
                        row.updated_at = _now()
                    session.flush()
                    return
            except IntegrityError:
                if attempt:  # pragma: no cover — the row exists by the retry
                    raise

    # ── Import watermark (RFC-0020 §3.6) ─────────────────────────────────────

    def get_watermark(self, project: str) -> datetime | None:
        """The ``since`` the next incremental import asks the provider for.

        ``None`` means "never imported": the caller does a full backfill.
        """
        stmt = select(ImportStateRow).where(
            ImportStateRow.tenant_id == (self._tenant or DEFAULT_TENANT),
            ImportStateRow.project == project,
        )
        with self._session() as session:
            row = session.scalars(stmt).first()
            return _as_utc(row.last_synced_at) if row is not None else None

    def set_watermark(self, project: str, when: datetime) -> None:
        """Record how far this project's import has read.

        Two concurrent first-ever imports both find no row and both insert; the
        unique index rejects the loser, which then updates instead. Same
        check-then-act race as the card upsert, and it is resolved the same way —
        by the constraint, not by hoping.
        """
        tenant = self._tenant or DEFAULT_TENANT
        stmt = select(ImportStateRow).where(
            ImportStateRow.tenant_id == tenant, ImportStateRow.project == project
        )
        for attempt in range(2):
            try:
                with self._session.begin() as session:
                    row = session.scalars(stmt).first()
                    if row is None:
                        session.add(
                            ImportStateRow(tenant_id=tenant, project=project, last_synced_at=when)
                        )
                    else:
                        row.last_synced_at = when
                return
            except IntegrityError:
                if attempt:  # pragma: no cover — the row exists by the retry
                    raise


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

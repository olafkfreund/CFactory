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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Index, Integer, Select, String, Text, func, select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, column_property, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from .audit import AuditStore
from .config import DEFAULT_TENANT, Settings, get_settings
from .credentials import (
    KEY_ENV,
    LEGACY_AAD_VERSION,
    Credential,
    CredentialError,
    CredentialInfo,
    GitCredentialRow,
    Sealed,
    env_credential,
    load_keyring,
    require_keyring,
    reseal,
    rewrap,
    seal,
    unseal,
)
from .db import Base, make_engine
from .git_config import (
    GitConfig,
    GitConfigError,
    GitConfigRow,
    GitConfigUpdate,
    GitTarget,
    config_view,
    provider_token,
    target_from_settings,
    validated_fields,
)
from .git_connections import (
    GitConnectionCreate,
    GitConnectionRow,
    GitConnectionUpdate,
    GitRepositoryCreate,
    GitRepositoryRow,
    GitRepositoryUpdate,
    GitResourceNotFoundError,
    ResolvedRepository,
    connection_fields,
    repository_fields,
    repository_patch,
    resolved_base_url,
    target_from_repository,
)
from .git_install import (
    CREDENTIAL_MISSING as INSTALL_DEGRADED,
)
from .git_install import (
    INSTALLED,
    GitInstallRow,
    InstallStateRow,
    InstallTokenSource,
    forget_token,
    new_state,
    state_digest,
    state_expiry,
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


def _issue_project(issue_ref: str | None) -> str | None:
    """The project path out of an ``owner/repo#123`` issue reference.

    How a card that carries an issue finds the repository that issue lives in —
    so a card imported from a GitLab repo syncs back to GitLab even when the
    tenant's default repository is on GitHub. Parsed here rather than imported
    from :mod:`cfactory.github_sync` because that module imports this one.
    """
    if not issue_ref or "#" not in issue_ref:
        return None
    return issue_ref.rsplit("#", 1)[0].strip() or None


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

    # ── Which repository this card is for (RFC-0020 §3.3, Phase 8) ───────────
    # NULL means "the tenant's default repository", which is what every card
    # created before this phase — and every card a human does not choose a
    # repository for — resolves to. Set it and the card syncs, imports and builds
    # against THAT repository on ITS connection, so two cards on one board can
    # target two repos on two different providers. Deliberately not a foreign key
    # with a cascade: deleting a repository must not delete planning data, so a
    # card pointing at a repository that is gone falls back to the default (see
    # ``resolve_repository``).
    repository_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Mirrored FROM GitHub, never pushed to it: GitHub is the record of truth for
    # these (see cfactory.github_sync — GitHub wins on conflict).
    issue_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Last sync failure, NULL when the last sync succeeded. A GitHub outage marks
    # the card here rather than raising: the board stays up and stays truthful
    # about the fact that it may now be stale.
    github_sync_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # ── Issue comments (Factory#375) ─────────────────────────────────────────
    # When this card's comment thread was last read IN FULL and successfully.
    # NULL is load-bearing and is the whole reason this column exists: without
    # it, "this issue has no discussion" and "this issue's discussion failed to
    # download" are the same zero rows in ``card_comments``, and the board would
    # present the second as the first. NULL means unknown — never fetched, or the
    # last attempt failed — and a non-NULL value means the stored thread is a
    # complete copy as of that instant. A failed fetch therefore leaves this
    # column, and the stored rows, exactly as they were.
    comments_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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


class CardCommentRow(Base):
    """One imported issue comment (Factory#375).

    A card's discussion, stored rather than fetched on card open. Storing was the
    open question when Factory#375 was filed and the missing precondition was a
    refresher: #374 shipped the per-repository incremental poll, so a stored copy
    now has something keeping it current, and the board gains a searchable,
    outage-surviving thread instead of a per-open provider call.

    **Idempotent on the provider-native comment id**, enforced by the unique
    index below rather than by an application-level "have I seen this?" check —
    the same reasoning as the card table's ``(tenant_id, issue_ref)`` guard: the
    check loses a race between two concurrent polls, the constraint does not. A
    re-import of an edited comment UPDATES the row; it never adds a second one.

    Comments are third-party text. ``body`` is stored verbatim — the board is not
    in the business of rewriting somebody's issue — and the safety lives at the
    render, where the cockpit's Markdown component emits React nodes and never
    HTML. Nothing here is ever interpolated into a query, a header or a URL.
    """

    __tablename__ = "card_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT
    )
    # The card this comment belongs to, by its stable human id. Not a foreign key,
    # for the same reason ``repository_id`` is not one: planning data outlives the
    # rows that reference it, and a cascade here would make a card delete destroy
    # imported discussion. A soft-deleted card's comments simply stop being read.
    card_key: Mapped[str] = mapped_column(String(128))
    # The PROVIDER's id for this comment, stringified — GitHub, GitLab and Azure
    # DevOps all number them independently, and the hub's normalised
    # ``IssueComment`` already hands it over as a string for exactly this use.
    comment_id: Mapped[str] = mapped_column(String(128))
    author: Mapped[str] = mapped_column(String(256), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    # The provider's timestamps, not ours: when the comment was written and last
    # edited THERE. The thread renders in ``created_at`` order.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_card_comments_tenant_card", "tenant_id", "card_key"),
        Index(
            "ix_card_comments_tenant_card_comment",
            "tenant_id",
            "card_key",
            "comment_id",
            unique=True,
        ),
    )


class CardComment(BaseModel):
    """Serialisable view of a :class:`CardCommentRow`."""

    model_config = ConfigDict(from_attributes=True)

    comment_id: str
    author: str
    body: str
    url: str
    created_at: datetime
    updated_at: datetime


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
    # Which repository this card targets, or NULL for the tenant's default
    # (RFC-0020 §3.3, phase 8).
    repository_id: int | None = None
    issue_state: str | None
    labels: list[str]
    github_sync_error: str | None
    # Read-only on the wire, like the mirrored GitHub columns: it records what
    # the factory was actually asked to do, so a caller must not be able to
    # assert a dispatch that never happened. Absent from CardCreate/CardUpdate.
    stage_runs: dict[str, Any]
    # The card's discussion, as two scalars rather than the thread itself
    # (Factory#375). The bodies are a separate GET — a backlog of 46 cards each
    # carrying a full comment thread is a payload nobody asked for, and the
    # cockpit renders the thread collapsed anyway. These two are what a client
    # needs BEFORE it decides to ask:
    #
    # * ``comment_count`` — how many are stored, so a card with no discussion
    #   renders no affordance at all;
    # * ``comments_synced_at`` — NULL when the thread has never been read
    #   successfully. ``comment_count == 0`` with a timestamp means "no
    #   discussion"; with NULL it means "we do not know", which is the honest
    #   answer for an issue whose comments failed to download.
    comment_count: int = 0
    comments_synced_at: datetime | None = None
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
    # Which of the tenant's repositories this card is for (RFC-0020 §3.3, phase
    # 8). Omit it for the tenant's default, which is what a board with one
    # repository — every board before this phase — always means.
    repository_id: int | None = None


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
    # Move the card to another of the tenant's repositories, or send ``null`` to
    # put it back on the tenant default (RFC-0020 §3.3, phase 8).
    repository_id: int | None = None


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
    # The incremental CURSOR: the newest issue ``updated_at`` seen, minus a clock
    # skew overlap. Not "when we last polled" — on a repository whose issues have
    # not changed in a month this stays a month old however often the poll runs,
    # which is why ``last_polled_at`` below exists as well (#374).
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When the provider was last read SUCCESSFULLY, by a poll or by a human. This
    # is the one the cockpit's staleness reads: it answers "is what I am looking at
    # current?", which the cursor above cannot.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Which poller currently owns this project's cycle, until when (#374). The
    # deployment runs more than one replica at times, and while a double import
    # cannot duplicate a card — the unique (tenant_id, issue_ref) index sees to
    # that — it does double the provider calls, which is the one thing the poll
    # must not do. NULL means unowned. See :meth:`CardStore.claim_import_lease`.
    poll_leased_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_card_import_state_tenant_project", "tenant_id", "project", unique=True),
    )


class ImportState(BaseModel):
    """How fresh one project's import is (#374) — the read side of the row above.

    Two timestamps because they answer two questions, and conflating them is what
    made staleness invisible: ``last_polled_at`` is when the provider was last read
    successfully (what a human means by "last synced"), ``watermark_at`` is how far
    that read has got (what the next incremental pass asks the provider for).
    """

    project: str
    last_polled_at: datetime | None = None
    watermark_at: datetime | None = None


# ``Card.comment_count``, computed by the DATABASE rather than maintained as a
# denormalised counter (Factory#375). A stored count is a second copy of a fact
# that can drift from the first one; a correlated subquery cannot. Declared here
# rather than inline on ``CardRow`` because it references ``CardCommentRow``,
# which is defined after it.
#
# ponytail: one scalar subquery per card row. A planning board is hundreds of
# rows, and both SQLite and PostgreSQL answer this from the
# ``(tenant_id, card_key)`` index; swap in a maintained counter only if a
# tenant's backlog ever gets big enough to measure.
CardRow.comment_count = column_property(
    select(func.count(CardCommentRow.id))
    .where(
        CardCommentRow.tenant_id == CardRow.tenant_id,
        CardCommentRow.card_key == CardRow.card_key,
    )
    .correlate_except(CardCommentRow)
    .scalar_subquery(),
    deferred=False,
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
    # Phase 8: which repository this card targets. NULL = the tenant default, so
    # every existing card keeps behaving exactly as it did.
    "repository_id": "INTEGER",
    # Factory#375: when this card's comment thread was last read in full. NULL on
    # every existing card, which is exactly what it means — never read — so the
    # next poll backfills rather than claiming an empty thread is complete.
    "comments_synced_at": "TIMESTAMP",
}

# The same guard for ``tenant_git_config``, which RFC-0020 phase 2 shipped and
# phase 3 gave one more column. A live board created by phase 2 already HAS the
# table, so ``create_all`` will not add it and every config read would fail.
_LATE_CONFIG_COLUMNS = {"credential_rejected": "BOOLEAN"}

# And for ``card_import_state``, which #374 gives the poll lease. A board created
# by §3.6 already HAS the table, so ``create_all`` will not add the column and
# every watermark read would fail.
_LATE_IMPORT_STATE_COLUMNS = {"poll_leased_until": "TIMESTAMP", "last_polled_at": "TIMESTAMP"}

# And for ``tenant_git_credential``, which phase 8 moves from per-tenant to
# per-connection. Both columns are added UNSET, which is exactly what a legacy
# record is: no connection yet, and the pre-phase-8 tenant-only crypto binding.
# ``adopt_legacy_git_config`` fills them in at boot.
_LATE_CREDENTIAL_COLUMNS = {"connection_id": "INTEGER", "aad_version": "INTEGER DEFAULT 1"}

# The per-tenant unique index phase 8 replaces: one credential per tenant is
# exactly the limitation being removed, so it has to GO on a live database and
# not merely be absent from a fresh one.
_DROPPED_INDEXES = ("ix_tenant_git_credential_tenant",)

# Indexes the late columns need. The unique one is the RFC-0020 §3.6 import
# idempotency guard and must exist on a live DB too, not only on a fresh
# create_all — without it two concurrent polls duplicate every card.
_LATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_cards_issue_ref ON cards (issue_ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_cards_tenant_id_issue_ref"
    " ON cards (tenant_id, issue_ref)",
    # Phase 8's replacement for the per-tenant credential index, on a table that
    # already exists — so create_all will never add it.
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_tenant_git_credential_connection"
    " ON tenant_git_credential (connection_id)",
    "CREATE INDEX IF NOT EXISTS ix_tenant_git_credential_tenant_id"
    " ON tenant_git_credential (tenant_id)",
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
    for table, columns in (
        (GitConfigRow.__tablename__, _LATE_CONFIG_COLUMNS),
        (GitCredentialRow.__tablename__, _LATE_CREDENTIAL_COLUMNS),
        (ImportStateRow.__tablename__, _LATE_IMPORT_STATE_COLUMNS),
    ):
        if not inspector.has_table(table):
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    for index in _DROPPED_INDEXES:
        # Its own transaction, and survivable: a database that never had the index
        # (a fresh create_all) must not fail boot because a DROP found nothing.
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DROP INDEX IF EXISTS {index}"))
        except Exception as exc:  # noqa: BLE001 — boot must survive; see below.
            logger.warning("could not drop legacy index %s: %s", index, exc)
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

    # ── Issue comments (Factory#375) ─────────────────────────────────────────

    def cards_for_project(self, project: str) -> Sequence[Card]:
        """This tenant's live cards imported from one project, oldest first.

        What the comment pass iterates. Scoped by the issue ref's project half
        rather than by ``repository_id`` because that is what the ref actually
        carries, and it is the same key :func:`_issue_project` and the per-card
        sync already resolve a card's host through — so a card imported from a
        GitLab project is only ever visited by the pass that runs on the GitLab
        connection.

        Soft-deleted cards are excluded by :meth:`_select`: a card off the board
        must not spend a provider call.
        """
        if not project.strip():
            return []
        stmt = (
            self._select()
            .where(CardRow.issue_ref.startswith(f"{project.strip()}#"))
            .order_by(CardRow.created_at.asc())
        )
        with self._session() as session:
            return [Card.model_validate(row) for row in session.scalars(stmt)]

    def comments(self, card_key: str) -> Sequence[CardComment]:
        """One card's stored discussion, oldest first.

        Tenant-scoped like every other read here: the filter is on this store's
        scope, so a card key from another tenant returns nothing rather than
        somebody else's thread.
        """
        stmt = (
            select(CardCommentRow)
            .where(CardCommentRow.card_key == card_key)
            .order_by(CardCommentRow.created_at.asc(), CardCommentRow.id.asc())
        )
        if self._tenant is not None:
            stmt = stmt.where(CardCommentRow.tenant_id == self._tenant)
        with self._session() as session:
            return [CardComment.model_validate(row) for row in session.scalars(stmt)]

    def store_comments(
        self, card_key: str, comments: Sequence[CardComment], *, synced_at: datetime
    ) -> int:
        """Upsert a card's comments and mark the thread complete as of ``synced_at``.

        **Idempotent on the provider-native comment id.** Re-importing an issue
        updates the rows it already has and adds only what is new — an edited
        comment changes in place, and no re-run can produce a second copy of one
        comment. The unique ``(tenant_id, card_key, comment_id)`` index is the
        guard; the lookup below is only the fast path.

        Called ONLY after a successful, complete read. A failed fetch must never
        reach here: setting ``comments_synced_at`` is the claim that what is
        stored is the whole thread, and making that claim for a download that
        failed is precisely the data loss Factory#375 reports. The caller
        therefore leaves both the rows and the marker untouched on failure, and
        the card keeps saying "unknown" rather than "no discussion".

        Returns how many rows were written (inserted or updated), for the tally.
        """
        tenant = self._tenant or DEFAULT_TENANT
        with self._session.begin() as session:
            existing = {
                row.comment_id: row
                for row in session.scalars(
                    select(CardCommentRow).where(
                        CardCommentRow.tenant_id == tenant,
                        CardCommentRow.card_key == card_key,
                    )
                )
            }
            for comment in comments:
                row = existing.get(comment.comment_id)
                if row is None:
                    session.add(
                        CardCommentRow(
                            tenant_id=tenant,
                            card_key=card_key,
                            **comment.model_dump(),
                        )
                    )
                    continue
                row.author = comment.author
                row.body = comment.body
                row.url = comment.url
                row.created_at = comment.created_at
                row.updated_at = comment.updated_at
            # ``updated_at`` is re-stated explicitly so the column's ``onupdate``
            # does not fire: a comment refresh is not an edit to the CARD, and a
            # board where every row looks freshly touched every five minutes
            # cannot answer "what changed recently".
            marker = (
                update(CardRow)
                .where(CardRow.card_key == card_key)
                .values(comments_synced_at=synced_at, updated_at=CardRow.updated_at)
            )
            if self._tenant is not None:
                marker = marker.where(CardRow.tenant_id == self._tenant)
            session.execute(marker)
        return len(comments)

    # ── Git connections and repositories (RFC-0020 §3.3, phase 8) ────────────
    #
    # Hung off the card store rather than given a store of its own, for the same
    # reason ``ImportStateRow`` is: it is *about* this tenant's cards, shares
    # their lifetime, and — decisively — every consumer of the git config
    # (github_sync, issue_import, card_intake) is already handed a tenant-scoped
    # CardStore. A second store would mean a second tenant-scoping mechanism to
    # keep in step with this one, which is precisely how a cross-tenant read gets
    # written by accident.
    #
    # Every read below filters on ``self.tenant`` and every write stamps it, so a
    # connection id or repository id from another tenant is NOT FOUND rather than
    # readable — the isolation is the same one the cards get, and the credential
    # binding (see cfactory.credentials) is the cryptographic backstop for when a
    # WHERE clause is wrong.

    def connections(self) -> Sequence[GitConnectionRow]:
        """This tenant's git connections, oldest first.

        ``Sequence`` rather than ``list`` because this class defines a ``list``
        METHOD, which shadows the builtin for every annotation below it.
        """
        stmt = (
            select(GitConnectionRow)
            .where(GitConnectionRow.tenant_id == self.tenant)
            .order_by(GitConnectionRow.id.asc())
        )
        with self._session() as session:
            return list(session.scalars(stmt))

    def connection(self, connection_id: int) -> GitConnectionRow:
        """One of this tenant's connections, or raise :class:`GitResourceNotFoundError`."""
        row = self._connection_or_none(connection_id)
        if row is None:
            raise GitResourceNotFoundError(f"no git connection {connection_id} for this tenant")
        return row

    def _connection_or_none(self, connection_id: int) -> GitConnectionRow | None:
        stmt = select(GitConnectionRow).where(
            GitConnectionRow.id == connection_id,
            GitConnectionRow.tenant_id == self.tenant,
        )
        with self._session() as session:
            return session.scalars(stmt).first()

    def create_connection(self, body: GitConnectionCreate) -> GitConnectionRow:
        """Add a connection. Raises ``GitConfigError`` if this host is already one.

        The duplicate is caught from the unique index rather than from a lookup:
        two concurrent creates of the same host both find nothing and both insert,
        and only the constraint settles that.
        """
        fields = connection_fields(body.provider, body.base_url, body.label)
        try:
            with self._session.begin() as session:
                row = GitConnectionRow(tenant_id=self.tenant, **fields)
                session.add(row)
                session.flush()
                return row
        except IntegrityError:
            raise GitConfigError(
                f"this tenant already has a {fields['provider']} connection to "
                f"{fields['base_url'] or 'the provider default host'} — edit that one, or add "
                "a repository to it"
            ) from None

    def update_connection(self, connection_id: int, body: GitConnectionUpdate) -> GitConnectionRow:
        """Patch a connection. Changing provider or host clears its verification."""
        sent = body.model_dump(exclude_unset=True)
        current = self.connection(connection_id)
        fields = connection_fields(
            body.provider if "provider" in sent else current.provider,
            body.base_url if "base_url" in sent else current.base_url,
            body.label if "label" in sent else current.label,
        )
        moved = (fields["provider"], fields["base_url"]) != (current.provider, current.base_url)
        if moved:
            # A verification proved the host that answered and the credential it
            # accepted. Point the connection somewhere else and it proves nothing.
            fields |= {"verified_at": None, "verify_error": None, "credential_rejected": None}
        try:
            with self._session.begin() as session:
                row = session.get(GitConnectionRow, connection_id)
                if row is None or row.tenant_id != self.tenant:  # pragma: no cover — just read
                    raise GitResourceNotFoundError(
                        f"no git connection {connection_id} for this tenant"
                    )
                for name, value in fields.items():
                    setattr(row, name, value)
                row.updated_at = _now()
                session.flush()
                return row
        except IntegrityError:
            raise GitConfigError(
                f"this tenant already has a {fields['provider']} connection to "
                f"{fields['base_url'] or 'the provider default host'}"
            ) from None

    def delete_connection(self, connection_id: int) -> bool:
        """Forget a connection, ITS REPOSITORIES and its credential.

        Everything hanging off a connection goes with it, in one transaction:
        a repository cannot be reached without the host it lives on, and a
        credential for a connection that no longer exists is a secret kept for no
        reason. Cards are NOT touched — a card whose repository is gone falls back
        to the tenant default, exactly like one that never named a repository.

        If the tenant's default repository was one of these, the oldest remaining
        repository is promoted, so a tenant that still has repositories always has
        a default to fall back to.
        """
        self.connection(connection_id)
        forget_token(self.tenant, connection_id)
        with self._session.begin() as session:
            for repo in session.scalars(
                select(GitRepositoryRow).where(GitRepositoryRow.connection_id == connection_id)
            ):
                session.delete(repo)
            for cred in session.scalars(
                select(GitCredentialRow).where(GitCredentialRow.connection_id == connection_id)
            ):
                session.delete(cred)
            # The install goes with it too: an installation_id for a connection
            # that no longer exists points at nothing, and leaving it would let a
            # recreated connection reusing the id inherit somebody's install.
            for install in session.scalars(
                select(GitInstallRow).where(GitInstallRow.connection_id == connection_id)
            ):
                session.delete(install)
            for pending in session.scalars(
                select(InstallStateRow).where(InstallStateRow.connection_id == connection_id)
            ):
                session.delete(pending)
            row = session.get(GitConnectionRow, connection_id)
            if row is not None:
                session.delete(row)
        self._ensure_default_repository()
        return True

    def repositories(self, connection_id: int | None = None) -> Sequence[GitRepositoryRow]:
        """This tenant's repositories, optionally only one connection's."""
        stmt = select(GitRepositoryRow).where(GitRepositoryRow.tenant_id == self.tenant)
        if connection_id is not None:
            stmt = stmt.where(GitRepositoryRow.connection_id == connection_id)
        with self._session() as session:
            return list(session.scalars(stmt.order_by(GitRepositoryRow.id.asc())))

    def repository(self, repository_id: int) -> GitRepositoryRow:
        """One of this tenant's repositories, or raise :class:`GitResourceNotFoundError`."""
        stmt = select(GitRepositoryRow).where(
            GitRepositoryRow.id == repository_id,
            GitRepositoryRow.tenant_id == self.tenant,
        )
        with self._session() as session:
            row = session.scalars(stmt).first()
        if row is None:
            raise GitResourceNotFoundError(f"no git repository {repository_id} for this tenant")
        return row

    def create_repository(self, connection_id: int, body: GitRepositoryCreate) -> GitRepositoryRow:
        """Add a repository to one of this tenant's connections.

        The first repository a tenant has becomes its default whatever
        ``make_default`` says: a tenant with repositories and no default would
        refuse every card that named none, which is not a state a create should be
        able to leave behind.
        """
        connection = self.connection(connection_id)
        fields = repository_fields(body, connection.provider)
        make_default = body.make_default or self.default_repository() is None
        try:
            with self._session.begin() as session:
                row = GitRepositoryRow(tenant_id=self.tenant, connection_id=connection_id, **fields)
                session.add(row)
                session.flush()
                created = row.id
        except IntegrityError:
            raise GitConfigError(
                f"{fields['project']!r} is already a repository on this connection"
            ) from None
        if make_default:
            return self.set_default_repository(created)
        return self.repository(created)

    def update_repository(self, repository_id: int, body: GitRepositoryUpdate) -> GitRepositoryRow:
        """Patch a repository. Only the fields actually sent are applied."""
        current = self.repository(repository_id)
        connection = self.connection(current.connection_id)
        fields = repository_patch(body, connection.provider)
        if not fields:
            return current
        try:
            with self._session.begin() as session:
                row = session.get(GitRepositoryRow, repository_id)
                if row is None or row.tenant_id != self.tenant:  # pragma: no cover — just read
                    raise GitResourceNotFoundError(
                        f"no git repository {repository_id} for this tenant"
                    )
                for name, value in fields.items():
                    setattr(row, name, value)
                row.updated_at = _now()
                session.flush()
                return row
        except IntegrityError:
            raise GitConfigError(
                f"{fields.get('project')!r} is already a repository on this connection"
            ) from None

    def delete_repository(self, repository_id: int) -> bool:
        """Forget a repository. Cards that pointed at it fall back to the default."""
        self.repository(repository_id)
        with self._session.begin() as session:
            row = session.get(GitRepositoryRow, repository_id)
            if row is not None:
                session.delete(row)
        self._ensure_default_repository()
        return True

    def set_default_repository(self, repository_id: int) -> GitRepositoryRow:
        """Make this repository the one a card that names none resolves to.

        Clearing the previous default and setting the new one happen in ONE
        transaction, because the database forbids two defaults per tenant (the
        unique ``default_for_tenant`` index) — so a two-step version would fail
        halfway and leave the tenant with none.
        """
        self.repository(repository_id)
        with self._session.begin() as session:
            for row in session.scalars(
                select(GitRepositoryRow).where(
                    GitRepositoryRow.default_for_tenant == self.tenant,
                    GitRepositoryRow.id != repository_id,
                )
            ):
                row.default_for_tenant = None
            session.flush()
            chosen = session.get(GitRepositoryRow, repository_id)
            if chosen is None:  # pragma: no cover — read above
                raise GitResourceNotFoundError(f"no git repository {repository_id} for this tenant")
            chosen.default_for_tenant = self.tenant
            session.flush()
            return chosen

    def _ensure_default_repository(self) -> None:
        """Promote the oldest repository when the tenant has lost its default."""
        remaining = self.repositories()
        if not remaining or any(repo.is_default for repo in remaining):
            return
        self.set_default_repository(remaining[0].id)

    def default_repository(self) -> ResolvedRepository | None:
        """The repository a card that names none resolves to, with its connection."""
        stmt = select(GitRepositoryRow).where(GitRepositoryRow.default_for_tenant == self.tenant)
        with self._session() as session:
            repo = session.scalars(stmt).first()
        if repo is None:
            return None
        connection = self._connection_or_none(repo.connection_id)
        if connection is None:  # pragma: no cover — deleted with its repositories
            return None
        return ResolvedRepository(connection, repo)

    def resolve_repository(
        self, *, repository_id: int | None = None, project: str | None = None
    ) -> ResolvedRepository | None:
        """The repository a card or an import means, or the tenant default.

        The resolution order, and the ONE place it is expressed:

        1. an explicit ``repository_id`` — a card that names its repository, or an
           import told exactly which one to read;
        2. a ``project`` path that matches one of this tenant's repositories — how
           a card that carries an ``issue_ref`` finds the repository that issue
           lives in, so a card imported from a GitLab repo syncs back to GitLab
           and not to whatever the tenant's default happens to be. The default
           wins the tie when two connections hold the same path, which is the
           only ambiguity possible here and the only answer that is stable;
        3. the tenant default.

        ``None`` only when the tenant has no repositories at all, which the caller
        renders as ``unconfigured`` (and, for a tenant still on the deployment's
        environment variables, falls back to those).
        """
        if repository_id is not None:
            repo = self.repository(repository_id)
            connection = self._connection_or_none(repo.connection_id)
            if connection is not None:
                return ResolvedRepository(connection, repo)
        wanted = (project or "").strip()
        if wanted:
            matches = [repo for repo in self.repositories() if repo.project == wanted]
            chosen = next(
                (repo for repo in matches if repo.is_default), matches[0] if matches else None
            )
            if chosen is not None:
                connection = self._connection_or_none(chosen.connection_id)
                if connection is not None:
                    return ResolvedRepository(connection, chosen)
        return self.default_repository()

    def git_target(
        self,
        settings: Settings | None = None,
        *,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> GitTarget:
        """This tenant's DEFAULT git target: its default repository, else the env.

        The ONE resolution every consumer uses when nothing names a repository —
        ``github_sync`` (which project an issue is opened in), ``issue_import``
        (which project issues are read from) and ``card_intake`` (which AIFactory
        project a card is built in). None of them looks at ``Settings`` for a
        provider, a repo or an intake project; if they did, the stored
        configuration would be a second opinion rather than the answer.

        It hangs off the store because the store is what knows the tenant: every
        consumer is already handed a tenant-scoped one, so tenant-correct
        configuration needs no tenant id threaded through five call signatures.

        ``actor`` and ``audit`` are stamped onto the audit entry the credential
        writes IF it is fetched (RFC-0020 §3.4). Resolving a target does not read
        a credential — the panel asks for a target on every poll and must not
        decrypt anything to answer — so a target that is never handed to a
        provider produces no entry.
        """
        return self.git_target_for(settings, actor=actor, audit=audit)

    def git_target_for(
        self,
        settings: Settings | None = None,
        *,
        repository_id: int | None = None,
        project: str | None = None,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> GitTarget:
        """The git target for one repository (see :meth:`resolve_repository`).

        A tenant with no repositories at all resolves against the deployment's
        environment variables, which is the one-release bridge phase 2 introduced
        and every unit test that never stores a configuration relies on.
        """
        settings = settings or get_settings()
        resolved = self.resolve_repository(repository_id=repository_id, project=project)
        if resolved is None:
            return target_from_settings(
                settings, self.tenant, self._tenant_credential(settings, actor=actor, audit=audit)
            )
        credential = self.connection_credential(
            resolved.connection.id, settings, actor=actor, audit=audit
        )
        return target_from_repository(resolved, settings, credential)

    def git_target_for_card(
        self,
        card: Card,
        settings: Settings | None = None,
        *,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> GitTarget:
        """The git target a CARD resolves to (RFC-0020 §3.3, phase 8).

        Its own repository if it names one, else the repository its issue lives in,
        else the tenant default — so two cards on one board can target two repos on
        two different providers, and a card that names nothing behaves exactly as
        it did before this phase.
        """
        return self.git_target_for(
            settings,
            repository_id=card.repository_id,
            project=_issue_project(card.issue_ref),
            actor=actor,
            audit=audit,
        )

    def seed_git_config_from_env(self, settings: Settings | None = None) -> GitConfig | None:
        """Materialise this tenant's config from the legacy env vars. Once.

        RFC-0020 §3.3: ``CFACTORY_INTAKE_PROJECT_ID`` (and, on the same rule, the
        ``CFACTORY_GITHUB_*`` / ``CFACTORY_GIT_PROVIDER_*`` project settings) are
        retired as globals but survive one release as a seed, so an existing
        single-tenant deployment keeps working with **no operator action** and
        its values become editable in the portal.

        Two rules make this safe to call on every boot:

        * a tenant that already has a CONNECTION is left ALONE — the stored
          configuration is authoritative, and re-seeding would silently undo an
          edit made in the cockpit every time the process restarted (the failure
          this is most likely to cause, and the one the tests pin);
        * nothing to seed (no project, no AIFactory project id) writes nothing, so
          a deploy that never configured any of this stays ``unconfigured``
          rather than acquiring an empty connection that reads as a deliberate
          choice.

        Since phase 8 what it materialises is one connection with one repository,
        marked as the tenant default — the same shape the legacy row is adopted
        into (:meth:`adopt_legacy_git_config`).

        Returns the seeded config, or ``None`` when it did nothing.
        """
        settings = settings or get_settings()
        if self.connections():
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

    # ── The single-configuration shim (RFC-0020 §3.3, phases 2-7) ────────────
    #
    # ``set_git_config`` / ``git_credential`` / ``record_git_verification`` are
    # what the pre-phase-8 REST endpoints, the pre-phase-8 MCP tools and the
    # environment seed call. They are now SHIMS over the two-level model rather
    # than a second place configuration is stored: each one operates on the
    # tenant's DEFAULT repository and the connection that repository lives on,
    # creating them if the tenant has none. There is no ``tenant_git_config`` row
    # written any more — that table is read once, by
    # :meth:`adopt_legacy_git_config`, and never again.

    def set_git_config(self, update: GitConfigUpdate, settings: Settings | None = None) -> None:
        """Replace this tenant's DEFAULT repository, and the connection it is on.

        The phase-2 PUT semantics, expressed on the new model: the provider and
        host become (or find) a connection, the project becomes the tenant's
        default repository on it, and any recorded verification is cleared —
        because it proved a configuration this one no longer is.

        A cleared ``project`` does NOT delete a repository. It clears the tenant's
        DEFAULT, so a card that names none is ``unconfigured`` again, and leaves
        every repository (and every card pointing at one) intact — a full-replace
        of one field is not a mandate to destroy the rest of the tenant's setup.
        """
        settings = settings or get_settings()
        fields = validated_fields(update)
        connection = self._shim_connection(str(fields["provider"]), fields["base_url"])
        # The phase-2 rule: any configuration write invalidates the verification.
        self._patch_connection(
            connection.id, {"verified_at": None, "verify_error": None, "credential_rejected": None}
        )
        project = fields["project"]
        if not project:
            self._clear_default_repository()
            return
        body = GitRepositoryCreate(
            project=str(project),
            intake_project=fields["intake_project"],
            aifactory_project_id=fields["aifactory_project_id"],
            default_labels=fields["default_labels"],
            make_default=True,
        )
        existing = next(
            (repo for repo in self.repositories(connection.id) if repo.project == str(project)),
            None,
        )
        if existing is None:
            self.create_repository(connection.id, body)
            return
        self.update_repository(
            existing.id,
            GitRepositoryUpdate(
                project=body.project,
                intake_project=body.intake_project,
                aifactory_project_id=body.aifactory_project_id,
                default_labels=body.default_labels,
            ),
        )
        self.set_default_repository(existing.id)

    def _shim_connection(self, provider: str, base_url: str | None) -> GitConnectionRow:
        """The connection a single-configuration write lands on.

        The phase-2 PUT replaced the tenant's ONE configuration, provider and host
        included, so on the new model it EDITS a connection rather than adding one:
        otherwise saving the panel with a different host would strand the tenant's
        credential (and its verification) on a connection nothing points at any
        more — the exact bug this method exists to prevent.

        In order: a connection that already names this (provider, host) is used as
        it is; otherwise the connection the tenant's default repository lives on —
        or its only connection — is moved to it; otherwise one is created. The
        credential survives the move, because it is bound to the connection's
        identity and not to its host.
        """
        wanted = connection_fields(provider, base_url, None)
        existing = list(self.connections())
        match = next(
            (
                row
                for row in existing
                if (row.provider, row.base_url or "") == (wanted["provider"], wanted["base_url"])
            ),
            None,
        )
        if match is not None:
            return match
        resolved = self.default_repository()
        editable = resolved.connection if resolved is not None else None
        if editable is None and len(existing) == 1:
            editable = existing[0]
        if editable is not None:
            try:
                return self.update_connection(
                    editable.id,
                    GitConnectionUpdate(
                        provider=str(wanted["provider"]), base_url=str(wanted["base_url"])
                    ),
                )
            except GitConfigError:  # pragma: no cover — another connection holds
                # that host, so the match above would have found it; kept because
                # a concurrent create could land between the two.
                pass
        return self._connection_for(provider, base_url)

    def _connection_for(self, provider: str, base_url: str | None) -> GitConnectionRow:
        """The tenant's connection for this (provider, host), created if absent.

        The get-or-create the shim needs. A direct create is tried first and the
        unique index decides: looking first and creating second loses the race
        between two concurrent saves of the same host.
        """
        url = base_url or None
        try:
            return self.create_connection(GitConnectionCreate(provider=provider, base_url=url))
        except GitConfigError:
            wanted = connection_fields(provider, url, None)
            found = next(
                (
                    row
                    for row in self.connections()
                    if (row.provider, row.base_url or "")
                    == (wanted["provider"], wanted["base_url"])
                ),
                None,
            )
            if found is None:  # pragma: no cover — the index rejected it, so it exists
                raise
            return found

    def _clear_default_repository(self) -> None:
        """Leave the tenant with no default repository (a cleared project)."""
        with self._session.begin() as session:
            for row in session.scalars(
                select(GitRepositoryRow).where(GitRepositoryRow.default_for_tenant == self.tenant)
            ):
                row.default_for_tenant = None

    def _default_connection(self, settings: Settings) -> GitConnectionRow:
        """The connection the single-configuration shim operates on.

        The default repository's connection, else the tenant's oldest connection,
        else one materialised for whatever provider the deployment's environment
        describes. A credential has to belong to a connection now — it is bound to
        one cryptographically — so storing one for a tenant that has configured
        nothing yet materialises the connection and NOT a repository: the tenant
        still reads as ``unconfigured``, which is what it is.
        """
        resolved = self.default_repository()
        if resolved is not None:
            return resolved.connection
        existing = self.connections()
        if existing:
            return existing[0]
        env = target_from_settings(settings, self.tenant)
        return self._connection_for(env.provider, env.base_url)

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

    def credential_row(self, connection_id: int) -> GitCredentialRow | None:
        """One connection's sealed credential row, or None if it has none.

        Scoped by tenant AS WELL AS by connection: the connection id came from a
        URL, and a tenant must not be able to read another tenant's sealed record
        by naming its id — even though the AAD binding means the record would not
        decrypt anyway.
        """
        stmt = select(GitCredentialRow).where(
            GitCredentialRow.connection_id == connection_id,
            GitCredentialRow.tenant_id == self.tenant,
        )
        with self._session() as session:
            return session.scalars(stmt).first()

    def connection_credential(
        self,
        connection_id: int,
        settings: Settings | None = None,
        *,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> Credential:
        """One connection's credential handle — never the credential.

        A stored credential is the answer whether or not it can currently be
        unsealed; it does NOT fall back to the deployment's environment token.
        Falling back would hand tenant A the operator's credential the moment
        tenant A's own record became unreadable, which is the cross-tenant leak
        phase 3 exists to close. Only a connection that has stored nothing uses
        the environment one.

        ``configured`` is answered WITHOUT decrypting: a row exists and this
        process holds the key that wraps it. A key of the right id but the wrong
        material therefore reads as configured and fails at fetch time, which the
        board reports as a rejected credential rather than as a green one — the
        alternative is decrypting a secret to render a boolean on every poll.
        """
        settings = settings or get_settings()
        # AN INSTALL WINS OVER A STORED CREDENTIAL (RFC-0020 §3.4 phase 4). Both
        # can be present — a GitLab install keeps its refresh token in exactly the
        # row below — and reading that row as the provider token would send a
        # refresh token where an access token is required. The install is also the
        # more current answer: a connection somebody installed an App on is not one
        # they still mean to reach with an older pasted PAT.
        install = self.install_row(connection_id)
        if install is not None:
            return self._install_credential(install, settings, actor=actor, audit=audit)
        row = self.credential_row(connection_id)
        if row is None:
            return env_credential(provider_token(settings))
        return Credential(
            CredentialInfo(
                configured=self._holds_key(row.key_version, settings),
                source="tenant",
                updated_at=_as_utc(row.updated_at),
                key_version=row.key_version,
            ),
            lambda: self._fetch_credential(connection_id, settings, actor=actor, audit=audit),
        )

    def git_credential(
        self,
        settings: Settings | None = None,
        *,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> Credential:
        """The single-configuration shim: the default connection's credential."""
        settings = settings or get_settings()
        return self._tenant_credential(settings, actor=actor, audit=audit)

    def _tenant_credential(
        self, settings: Settings, *, actor: str = SYSTEM_ACTOR, audit: AuditStore | None = None
    ) -> Credential:
        """The credential of the tenant's default connection, else the env one.

        Used where no connection is named: the pre-phase-8 endpoints and a tenant
        that resolves entirely from the deployment's environment variables. A
        tenant with connections but no default repository falls back to its OLDEST
        connection, which for the single-connection deployment every phase-3
        install is means "the tenant's credential", unchanged.
        """
        resolved = self.default_repository()
        connection = resolved.connection if resolved is not None else None
        if connection is None:
            existing = self.connections()
            connection = existing[0] if existing else None
        if connection is None:
            return env_credential(provider_token(settings))
        return self.connection_credential(connection.id, settings, actor=actor, audit=audit)

    def _holds_key(self, key_version: str, settings: Settings) -> bool:
        """Whether this process holds the KEK that wraps *key_version*."""
        try:
            keyring = load_keyring(settings)
        except CredentialError as exc:
            logger.error("credential key is unusable for tenant %s: %s", self.tenant, exc)
            return False
        return keyring is not None and keyring.find(key_version) is not None

    def _fetch_credential(
        self, connection_id: int, settings: Settings, *, actor: str, audit: AuditStore | None
    ) -> str | None:
        """Unseal one connection's credential for ONE provider call, and audit it.

        Every outcome is chained, including the failures: "the credential could
        not be read at 14:02" is precisely the entry an operator needs when a
        board goes quiet after a key rotation, and an audit trail that only
        records the successes cannot answer that.

        Never raises. A missing key, an unusable key or an altered record yields
        no credential, which the board renders as ``credential_missing`` and
        keeps serving — a credential problem degrades the board, it does not take
        it down.
        """
        row = self.credential_row(connection_id)
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
            secret = unseal(
                row.sealed(), tenant=self.tenant, connection=connection_id, keyring=keyring
            )
        except CredentialError as exc:
            # The message names the tenant and the failure, never the record and
            # never a fragment of the credential.
            logger.error("credential read failed for tenant %s: %s", self.tenant, exc)
            self._audit_credential(audit, actor, kind="read_git_credential", ok=False)
            return None
        self._audit_credential(audit, actor, kind="read_git_credential", ok=True)
        self._migrate_sealed(row, connection_id, keyring)
        return secret

    def _migrate_sealed(self, row: GitCredentialRow, connection_id: int, keyring: Any) -> None:
        """Move a record onto the active KEK and the current binding, if needed.

        Two migrations, both lazy and both invisible:

        * **KEK rotation** — put the new key FIRST in ``CFACTORY_CREDENTIAL_KEY``,
          keep the old one listed, and records move as they are used. The
          credential is not decrypted to do it: only its data key is re-wrapped
          (see :func:`cfactory.credentials.rewrap`).
        * **the phase-8 binding** — a record sealed before connections existed is
          bound to the tenant only. Re-sealing it onto (tenant, connection) DOES
          decrypt, once, in memory (:func:`cfactory.credentials.reseal`); the
          plaintext is never returned from that call, never logged and never
          written anywhere but back into the sealed columns.

        ponytail: lazy, on read, plus an eager sweep at boot
        (:meth:`adopt_legacy_git_config`) — so a credential nothing ever uses
        still gets its binding upgraded. Check every connection reports the new
        ``key_version`` in the panel before dropping an old key from the
        environment.
        """
        try:
            current = row.sealed()
            rewrapped = rewrap(
                current, tenant=self.tenant, connection=connection_id, keyring=keyring
            )
            if rewrapped is not None:
                current = rewrapped
                self._store_sealed(connection_id, rewrapped)
                logger.info(
                    "re-wrapped tenant %s connection %s credential onto key %s",
                    self.tenant,
                    connection_id,
                    rewrapped.key_version,
                )
            resealed = reseal(
                current, tenant=self.tenant, connection=connection_id, keyring=keyring
            )
            if resealed is not None:
                self._store_sealed(connection_id, resealed)
                logger.info(
                    "re-sealed tenant %s connection %s credential onto the connection binding "
                    "(RFC-0020 phase 8)",
                    self.tenant,
                    connection_id,
                )
        except CredentialError as exc:  # pragma: no cover — unsealing just succeeded
            logger.warning(
                "could not migrate tenant %s connection %s credential: %s",
                self.tenant,
                connection_id,
                exc,
            )

    def sealed_for(self, connection_id: int) -> Sealed | None:
        """The sealed record for a connection, as the crypto layer sees it."""
        row = self.credential_row(connection_id)
        return row.sealed() if row is not None else None

    def set_connection_credential(
        self, connection_id: int, secret: str, settings: Settings | None = None
    ) -> CredentialInfo:
        """Store (or replace) one connection's credential, encrypted.

        FAILS CLOSED: with no ``CFACTORY_CREDENTIAL_KEY`` configured this raises
        :class:`~cfactory.credentials.CredentialError` rather than writing
        anything. There is no plaintext path, not even a degraded one.

        Sealed against (tenant, connection), so the record is unusable on any
        other connection — including another of this tenant's.
        """
        settings = settings or get_settings()
        connection = self.connection(connection_id)
        value = (secret or "").strip()
        if not value:
            raise CredentialError("credential must not be empty")
        sealed = seal(
            value, tenant=self.tenant, connection=connection.id, keyring=require_keyring(settings)
        )
        self._store_sealed(connection.id, sealed)
        # A new credential makes any recorded rejection obsolete — it was about
        # the credential this one replaces.
        self._patch_connection(connection.id, {"credential_rejected": None})
        return CredentialInfo(
            configured=True,
            source="tenant",
            updated_at=_now(),
            key_version=sealed.key_version,
        )

    def set_git_credential(self, secret: str, settings: Settings | None = None) -> CredentialInfo:
        """The single-configuration shim: store the DEFAULT connection's credential.

        Materialises a connection for a tenant that has none (see
        :meth:`_default_connection`) because a credential has to belong to one now.
        It materialises no repository, so the tenant still reads as
        ``unconfigured`` — storing a credential has never been allowed to invent a
        project that then looks like a deliberate choice.
        """
        settings = settings or get_settings()
        return self.set_connection_credential(
            self._default_connection(settings).id, secret, settings
        )

    def clear_connection_credential(self, connection_id: int) -> bool:
        """Forget one connection's credential. True if there was one.

        The revocation path: a credential that has leaked has to be removable
        from the surface that stored it, not by an operator with a SQL client.
        """
        self.connection(connection_id)
        with self._session.begin() as session:
            row = session.scalars(
                select(GitCredentialRow).where(
                    GitCredentialRow.connection_id == connection_id,
                    GitCredentialRow.tenant_id == self.tenant,
                )
            ).first()
            if row is None:
                return False
            session.delete(row)
            return True

    def clear_git_credential(self) -> bool:
        """The single-configuration shim: forget the DEFAULT connection's credential."""
        resolved = self.default_repository()
        existing = self.connections()
        connection = (
            resolved.connection if resolved is not None else (existing[0] if existing else None)
        )
        if connection is None:
            return False
        return self.clear_connection_credential(connection.id)

    def _store_sealed(self, connection_id: int, sealed: Sealed) -> None:
        """Insert-or-update one connection's sealed credential.

        Constraint, not check: two concurrent first-ever writes both find no row
        and both insert, and the unique index rejects the loser, which then takes
        the update path.
        """
        stmt = select(GitCredentialRow).where(
            GitCredentialRow.connection_id == connection_id,
            GitCredentialRow.tenant_id == self.tenant,
        )
        for attempt in range(2):
            try:
                with self._session.begin() as session:
                    row = session.scalars(stmt).first()
                    if row is None:
                        session.add(
                            GitCredentialRow(
                                tenant_id=self.tenant,
                                connection_id=connection_id,
                                key_version=sealed.key_version,
                                aad_version=sealed.aad_version,
                                wrapped_key=sealed.wrapped_key,
                                ciphertext=sealed.ciphertext,
                            )
                        )
                    else:
                        row.key_version = sealed.key_version
                        row.aad_version = sealed.aad_version
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

    # ── The install flow (RFC-0020 §3.4, phase 4) ────────────────────────────
    #
    # An install is the OTHER way a connection becomes authenticated: instead of a
    # human pasting a token, a human clicks through the provider's own consent
    # screen and what lands here is an ``installation_id`` (GitHub — not a secret)
    # or a sealed refresh token (GitLab, in the phase-3 store above). The
    # per-invocation token is minted by :class:`~cfactory.git_install.
    # InstallTokenSource`, which is handed the three store-shaped things it needs
    # as callbacks so that module never has to import this one.

    def install_row(self, connection_id: int) -> GitInstallRow | None:
        """One connection's install, or None. Scoped by tenant as well as id."""
        stmt = select(GitInstallRow).where(
            GitInstallRow.connection_id == connection_id,
            GitInstallRow.tenant_id == self.tenant,
        )
        with self._session() as session:
            return session.scalars(stmt).first()

    def _install_credential(
        self,
        install: GitInstallRow,
        settings: Settings,
        *,
        actor: str = SYSTEM_ACTOR,
        audit: AuditStore | None = None,
    ) -> Credential:
        """The credential handle for an INSTALLED connection.

        ``configured`` is the install's own status, so a connection whose last
        refresh failed reports ``credential_missing`` on the next panel poll
        WITHOUT anything having to mint again — which is what makes the
        degradation visible rather than only discoverable by trying a board write.

        ``fetch`` mints. Nothing is decrypted or minted to answer ``info``.
        """
        connection = self._connection_or_none(install.connection_id)
        base_url = resolved_base_url(
            install.provider, (connection.base_url if connection is not None else "") or ""
        )
        source = InstallTokenSource(
            tenant=self.tenant,
            connection_id=install.connection_id,
            provider=install.provider,
            installation_id=install.installation_id,
            base_url=base_url,
            settings=settings,
            read_secret=lambda: self._fetch_credential(
                install.connection_id, settings, actor=actor, audit=audit
            ),
            write_secret=lambda secret: self.store_install_secret(install.connection_id, secret),
            degrade=lambda reason: self.degrade_install(install.connection_id, reason),
            recover=lambda: self.recover_install(install.connection_id),
        )
        return Credential(
            CredentialInfo(
                configured=install.status == INSTALLED,
                # A THIRD source word beside "tenant" and "env", because it is a
                # third thing: nothing reusable was stored, and the panel must not
                # offer to "remove the credential" for a connection whose answer is
                # to disconnect the install.
                source="install",
                updated_at=_as_utc(install.updated_at),
            ),
            source.token,
        )

    def start_install(self, connection_id: int, redirect_uri: str) -> str:
        """Begin an install for one of THIS tenant's connections. Returns the state.

        The returned token is the only time it exists in this process: what is
        stored is its SHA-256, so the row a callback matches against cannot be
        turned back into a state by anyone who reads the database.

        The connection is looked up through :meth:`connection`, which is
        tenant-scoped — so a tenant cannot start an install against another
        tenant's connection, and the state row it would need therefore never
        exists.
        """
        connection = self.connection(connection_id)
        state = new_state()
        with self._session.begin() as session:
            session.add(
                InstallStateRow(
                    state_hash=state_digest(state),
                    tenant_id=self.tenant,
                    connection_id=connection.id,
                    provider=connection.provider,
                    redirect_uri=redirect_uri,
                    expires_at=state_expiry(),
                )
            )
        return state

    def consume_install_state(self, state: str) -> InstallStateRow | None:
        """Match a callback's state, ONCE. Deliberately not tenant-scoped.

        A provider redirect carries no session and no ``X-Tenant-Id`` — so this
        lookup cannot be scoped by a tenant, and the tenant is instead READ OFF the
        row it finds. That is the whole cross-tenant guarantee: the tenant a
        callback lands on is the tenant that started the install, and nothing the
        request says can move it.

        Single-use: the row is deleted inside the same transaction that reads it,
        so a replayed callback URL matches nothing. Expired states are deleted and
        report as no match, which is the same answer a forged one gets.
        """
        if not (state or "").strip():
            return None
        digest = state_digest(state)
        with self._session.begin() as session:
            row = session.scalars(
                select(InstallStateRow).where(InstallStateRow.state_hash == digest)
            ).first()
            if row is None:
                return None
            # Copied out and the stored row deleted in the same transaction, so
            # what the caller inspects can never be re-matched by a second
            # callback carrying the same URL.
            claimed = InstallStateRow(
                state_hash=row.state_hash,
                tenant_id=row.tenant_id,
                connection_id=row.connection_id,
                provider=row.provider,
                redirect_uri=row.redirect_uri,
                expires_at=row.expires_at,
            )
            session.delete(row)
        expires_at = _as_utc(claimed.expires_at)
        # A row with no expiry cannot be shown to be live, so it is treated as
        # expired. Fail-closed: the column is NOT NULL and this is unreachable,
        # and if it ever became reachable the safe answer is to refuse.
        if expires_at is None or expires_at < _now():
            logger.info("install state for connection %s expired unused", claimed.connection_id)
            return None
        return claimed

    def record_install(
        self, connection_id: int, provider: str, *, installation_id: str | None, account: str | None
    ) -> GitInstallRow:
        """Attach a completed install to one of this tenant's connections.

        Replaces any previous install on the connection — reconnecting after
        revoking at the provider is the normal repair, and it must not need a
        delete first. Any cached token for the connection is dropped, since it was
        minted from the install being replaced.
        """
        connection = self.connection(connection_id)
        forget_token(self.tenant, connection_id)
        with self._session.begin() as session:
            row = session.scalars(
                select(GitInstallRow).where(
                    GitInstallRow.connection_id == connection_id,
                    GitInstallRow.tenant_id == self.tenant,
                )
            ).first()
            if row is None:
                row = GitInstallRow(tenant_id=self.tenant, connection_id=connection.id)
                session.add(row)
            row.provider = provider
            row.installation_id = installation_id
            row.account = account
            row.status = INSTALLED
            row.error = None
            row.updated_at = _now()
            session.flush()
            session.expunge(row)
        return row

    def delete_install(self, connection_id: int) -> bool:
        """Disconnect an install. True if there was one.

        The GitLab refresh token goes with it — a refresh token for an install
        nothing points at is a secret kept for no reason — and so does any cached
        token. This does NOT revoke at the provider: uninstalling the App is done
        on the provider's own settings page, and the runbook says so.
        """
        self.connection(connection_id)
        forget_token(self.tenant, connection_id)
        with self._session.begin() as session:
            row = session.scalars(
                select(GitInstallRow).where(
                    GitInstallRow.connection_id == connection_id,
                    GitInstallRow.tenant_id == self.tenant,
                )
            ).first()
            if row is None:
                return False
            session.delete(row)
        self.clear_connection_credential(connection_id)
        return True

    def degrade_install(self, connection_id: int, reason: str) -> None:
        """Mark an install unusable, with why (RFC-0020 §3.4).

        The requirement in one method: a refresh that FAILS drops the tenant to
        ``credential_missing`` — because ``_install_credential`` reports
        ``configured=False`` for any status but ``installed``, and
        :func:`~cfactory.git_config.derive_status` turns that into exactly that
        word — and the reason is stored where the Settings panel renders it.
        """
        self._patch_install(connection_id, status=INSTALL_DEGRADED, error=reason[:512])

    def recover_install(self, connection_id: int) -> None:
        """Clear a degradation after a mint works again. A no-op when there is none."""
        row = self.install_row(connection_id)
        if row is not None and row.status != INSTALLED:
            self._patch_install(connection_id, status=INSTALLED, error=None)

    def _patch_install(self, connection_id: int, *, status: str, error: str | None) -> None:
        with self._session.begin() as session:
            row = session.scalars(
                select(GitInstallRow).where(
                    GitInstallRow.connection_id == connection_id,
                    GitInstallRow.tenant_id == self.tenant,
                )
            ).first()
            if row is None:
                return
            row.status = status
            row.error = error
            row.updated_at = _now()

    def store_install_secret(self, connection_id: int, secret: str) -> None:
        """Seal the ONE value an install may keep — GitLab's refresh token.

        A thin pass-through to the phase-3 store on purpose: the install flow gets
        no second credential table and no second crypto path. What may be handed
        here is decided by
        :func:`~cfactory.git_install.persistable_secret`, and a GitHub
        installation token cannot reach this method because that function raises
        rather than returning one.
        """
        self.set_connection_credential(connection_id, secret)

    def record_connection_verification(
        self, connection_id: int, *, error: str | None, rejected: bool | None = None
    ) -> None:
        """Record what a verify proved about one connection."""
        self._patch_connection(
            connection_id,
            {
                "verified_at": None if error else _now(),
                "verify_error": error,
                # A successful verify proves the credential was ACCEPTED, so it
                # clears any earlier rejection as well as recording the success.
                "credential_rejected": bool(rejected) if error else None,
            },
        )

    def record_git_verification(
        self,
        *,
        error: str | None,
        rejected: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        """The single-configuration shim: record a verify on the default connection.

        Materialises the connection when the tenant is still resolving from the
        environment: asking to verify is asking about a *specific* configuration,
        and the answer has to be recorded against something. What is materialised
        is exactly what the seed would have written, so this cannot invent a
        configuration the deployment did not already describe.
        """
        settings = settings or get_settings()
        if not self.connections():
            env = target_from_settings(settings, self.tenant)
            self.set_git_config(
                GitConfigUpdate(
                    provider=env.provider,
                    base_url=env.base_url,
                    project=env.project,
                    aifactory_project_id=env.aifactory_project_id,
                ),
                settings,
            )
        self.record_connection_verification(
            self._default_connection(settings).id, error=error, rejected=rejected
        )

    def _patch_connection(self, connection_id: int, fields: dict[str, Any]) -> None:
        """Write bookkeeping columns onto a connection this tenant owns."""
        with self._session.begin() as session:
            row = session.get(GitConnectionRow, connection_id)
            if row is None or row.tenant_id != self.tenant:
                raise GitResourceNotFoundError(f"no git connection {connection_id} for this tenant")
            for name, value in fields.items():
                setattr(row, name, value)
            row.updated_at = _now()

    # ── Adopting the phase-2 single row (RFC-0020 §3.3, phase 8) ─────────────

    def adopt_legacy_git_config(self, settings: Settings | None = None) -> int:
        """Turn every pre-phase-8 ``tenant_git_config`` row into a connection.

        Runs at boot, for EVERY tenant in the database rather than for this
        store's own — an upgrade must not wait for a tenant to log in — and is
        idempotent: a tenant that already has a connection is skipped, so a
        restart never re-adopts and never overwrites an edit made in the cockpit.

        One legacy row becomes exactly one connection (provider, base_url, verify
        state) plus, when it named a project, one repository marked as the tenant
        default (project, intake_project, aifactory_project_id, default_labels).

        **The credential is re-sealed here, never re-typed and never written out.**
        The legacy record is bound to the tenant only; this binds it to the tenant
        AND the new connection. It is unsealed and re-sealed in memory
        (:func:`cfactory.credentials.reseal`) and the plaintext is not returned,
        not logged and not written anywhere but back into the sealed columns. When
        this deployment holds no usable key the record is LEFT ALONE, still marked
        legacy and still readable by the legacy path the moment the key is back —
        a missing key must not destroy a credential, and it cannot be re-sealed
        without one. Returns how many tenants were adopted.

        Why here and not in the Alembic migration: this deployment bootstraps its
        schema with ``create_all`` (see ``_ensure_late_columns``) and may never run
        Alembic at all, and — decisively — a migration process is not guaranteed
        to hold ``CFACTORY_CREDENTIAL_KEY``, so a re-seal there would either fail
        the upgrade or silently skip the credential. The app process has the key by
        definition: without it, it cannot read the credential either.
        """
        settings = settings or get_settings()
        with self._session() as session:
            legacy = list(session.scalars(select(GitConfigRow)))
        adopted = 0
        for row in legacy:
            scoped = self.scoped(row.tenant_id)
            if scoped.connections():
                continue
            scoped._adopt_one(row, settings)
            adopted += 1
        return adopted

    def _adopt_one(self, row: GitConfigRow, settings: Settings) -> None:
        """Adopt ONE legacy configuration row into this (scoped) store's tenant."""
        provider = (row.provider or "github").strip().lower()
        connection = self.create_connection(
            GitConnectionCreate(provider=provider, base_url=row.base_url)
        )
        self._patch_connection(
            connection.id,
            {
                "verified_at": row.verified_at,
                "verify_error": row.verify_error,
                "credential_rejected": row.credential_rejected,
            },
        )
        if row.project:
            self.create_repository(
                connection.id,
                GitRepositoryCreate(
                    project=row.project,
                    intake_project=row.intake_project,
                    aifactory_project_id=row.aifactory_project_id,
                    default_labels=[
                        label for label in (row.default_labels or []) if isinstance(label, str)
                    ],
                    make_default=True,
                ),
            )
        logger.info(
            "adopted tenant %r legacy git config into connection %s (provider=%s project=%s)",
            self.tenant,
            connection.id,
            provider,
            row.project,
        )
        self._adopt_credential(connection.id, settings)

    def _adopt_credential(self, connection_id: int, settings: Settings) -> None:
        """Attach the tenant's legacy credential to its new connection, re-sealed.

        Never logs, returns or stores the plaintext — see
        :meth:`adopt_legacy_git_config`. A record that cannot be unsealed keeps its
        legacy binding rather than being deleted or rewritten.
        """
        stmt = select(GitCredentialRow).where(
            GitCredentialRow.tenant_id == self.tenant,
            GitCredentialRow.connection_id.is_(None),
        )
        with self._session.begin() as session:
            row = session.scalars(stmt).first()
            if row is None:
                return
            row.connection_id = connection_id
            row.aad_version = LEGACY_AAD_VERSION
        sealed = self.sealed_for(connection_id)
        if sealed is None:  # pragma: no cover — just written
            return
        try:
            keyring = load_keyring(settings)
            if keyring is None:
                raise CredentialError(f"no {KEY_ENV} is set")
            resealed = reseal(sealed, tenant=self.tenant, connection=connection_id, keyring=keyring)
        except CredentialError as exc:
            logger.warning(
                "tenant %s credential kept its pre-phase-8 binding (%s); it will be re-sealed "
                "on the first read once the key is available",
                self.tenant,
                exc,
            )
            return
        if resealed is not None:
            self._store_sealed(connection_id, resealed)
            logger.info(
                "re-sealed tenant %s credential onto connection %s", self.tenant, connection_id
            )

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
        """Record how far this project's import has read."""
        self._upsert_import_state(project, last_synced_at=when)

    def _upsert_import_state(self, project: str, **fields: datetime | None) -> None:
        """Write import-state fields for this (tenant, project), creating the row.

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
                        row = ImportStateRow(tenant_id=tenant, project=project)
                        session.add(row)
                    for name, value in fields.items():
                        setattr(row, name, value)
                return
            except IntegrityError:
                if attempt:  # pragma: no cover — the row exists by the retry
                    raise

    def import_states(self) -> dict[str, ImportState]:
        """Every project this tenant has imported from, and how fresh each is.

        The read behind ``GET /api/cards/sync-state`` (#374). A project with a
        configured repository and no row here has never been imported, which the
        caller renders as "never" rather than as an error — never-synced and
        synced-long-ago are different states and a board must show which it is in.
        """
        stmt = select(ImportStateRow).where(
            ImportStateRow.tenant_id == (self._tenant or DEFAULT_TENANT)
        )
        with self._session() as session:
            return {
                row.project: ImportState(
                    project=row.project,
                    last_polled_at=_as_utc(row.last_polled_at),
                    watermark_at=_as_utc(row.last_synced_at),
                )
                for row in session.scalars(stmt)
            }

    def mark_polled(self, project: str, when: datetime | None = None) -> None:
        """Record a SUCCESSFUL read of this project's issues (#374).

        Separate from :meth:`set_watermark` because a successful pass that found
        nothing new has no watermark to advance and must still count as synced —
        without this, a quiet repository would read as permanently stale in the
        cockpit and every board would look broken between issue filings.
        """
        self._upsert_import_state(project, last_polled_at=when or datetime.now(UTC))

    def claim_import_lease(self, project: str, ttl_seconds: float) -> bool:
        """Claim this project's next poll cycle. True if this caller owns it (#374).

        The multi-replica guard. Two replicas both wake up, both enumerate the same
        repositories, and both would read the same issues from the same host — a
        stampede that is invisible on the board (the import is an upsert) and very
        visible in a rate-limit budget. Whoever claims the lease polls; the other
        skips this cycle.

        Compare-and-set on the value just read, NOT a ``leased_until < now``
        predicate: both replicas read the same previous value, both attempt the
        UPDATE, and the second one's WHERE no longer matches — so the database
        decides, and the expiry arithmetic stays in Python where the naive/aware
        timestamp handling is already solved (``_as_utc``). One winner without
        needing the two backends to agree on how to compare a stored datetime.

        The lease is advisory and expiring: a replica that dies mid-cycle blocks
        this project for at most ``ttl_seconds``, after which the next poller takes
        it. It is not a correctness boundary — the unique ``(tenant_id, issue_ref)``
        index is — so a lost lease costs one duplicate READ, never a duplicate card.
        """
        now = datetime.now(UTC)
        tenant = self._tenant or DEFAULT_TENANT
        stmt = select(ImportStateRow).where(
            ImportStateRow.tenant_id == tenant, ImportStateRow.project == project
        )
        with self._session.begin() as session:
            row = session.scalars(stmt).first()
            if row is None:
                # First-ever poll of this project. A lost race here is resolved by
                # the unique index exactly as in set_watermark: the loser sees the
                # row on its next attempt and takes the UPDATE path.
                try:
                    session.add(
                        ImportStateRow(
                            tenant_id=tenant,
                            project=project,
                            poll_leased_until=now + timedelta(seconds=ttl_seconds),
                        )
                    )
                    session.flush()
                except IntegrityError:  # pragma: no cover — needs two live pollers
                    return False
                return True
            previous = _as_utc(row.poll_leased_until)
            if previous is not None and previous > now:
                return False  # somebody else owns this cycle
            # Cast because ``Session.execute`` is typed as returning ``Result``,
            # which has no ``rowcount``; an UPDATE always yields a ``CursorResult``,
            # and the row count is precisely what decides the winner here.
            claimed = cast(
                "CursorResult[Any]",
                session.execute(
                    update(ImportStateRow)
                    .where(
                        ImportStateRow.id == row.id,
                        ImportStateRow.poll_leased_until == row.poll_leased_until,
                    )
                    .values(poll_leased_until=now + timedelta(seconds=ttl_seconds))
                ),
            )
            return claimed.rowcount == 1


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

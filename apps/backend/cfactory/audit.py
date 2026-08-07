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

An append reads the chain tail and then inserts, so the two must be one
critical section or concurrent appends fork the chain (#306). See
:func:`_serialise_sqlite_appends` and :func:`_lock_chain` for how that is held,
and :meth:`AuditStore.check` for how a fork left by the pre-fix code is
classified — as a structural anomaly, distinct from tamper evidence.

:meth:`AuditStore.report` turns that classification into the one verdict an
operator reads (#309), served at ``GET /api/audit/chain``. Before it existed,
seeing the chain's state meant ``kubectl exec`` into the pod — which is how the
#306 false alarm ran for a week with nobody looking.

Mirrors :mod:`cfactory.store`: an ORM row on the shared ``Base`` plus a thin
repository with an injectable ``url`` (a temp SQLite file for tests, PostgreSQL
in real deployments).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Boolean, DateTime, Integer, String, event, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from .auth import get_keystore, key_actor
from .config import Settings, get_settings
from .db import Base, make_engine
from .models import as_utc


def _now() -> datetime:
    return datetime.now(UTC)


# The credential shapes this fleet issues: CFactory's own `cfk_…` and the
# AIFactory-style `acw_…` (the one #251 was filed about). Anchored and demanding
# real length, so it cannot match the actors this seam legitimately produces —
# `system`, `local`, `user:<email>`, `unattributed:key-<digest>` — none of which
# is a bare prefixed token. Widen this if a new key prefix is minted.
_KEY_SHAPED = re.compile(r"^(?:acw|cfk)_[A-Za-z0-9]{16,}$")


def redact_actor(actor: str) -> str:
    """Replace a live API key standing as the actor with a safe reference (#251).

    :func:`cfactory.enterprise.identity_dep` already maps a presented key to a
    non-reversible reference, so on the WRITE path this is the backstop that
    stops any other caller — a future route, an MCP tool, a test harness — from
    landing a working credential in the trail.

    Applied on the READ path too, because rows written before that fix still
    hold the raw key: this keeps them out of ``GET /api/audit`` and off the
    Audit view. It does NOT clean the copy at rest. Entries are HMAC-chained
    (:func:`compute_entry_hash`), so rewriting a stored actor is
    indistinguishable from tampering and would break :meth:`AuditStore.verify`
    for every later entry — retiring the key is the remediation for the rows
    already written, not a migration.

    Two conditions, and the second exists because the first alone makes the
    remediation backfire. Redacting only a CURRENTLY configured key is the
    property that matters for *security* — no live credential is served. But
    the fix for the rows already written is to RETIRE the key, and the moment
    it leaves ``CFACTORY_API_KEYS`` those rows stop matching and the trail
    serves the stored string verbatim again. It is dead by then, but it is a
    40-character opaque token rendered in the Audit view, indistinguishable on
    screen (or in a screenshot, or an export) from the live one that started
    #251. Shape is therefore checked as well as configuration, so rotating
    cleans the trail up instead of un-redacting it.
    """
    if get_keystore().scopes_for(actor) is not None or _KEY_SHAPED.match(actor):
        return key_actor(actor)
    return actor


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


# Advisory-lock key for the audit chain on PostgreSQL. Arbitrary but fixed:
# every appender must ask for the same key or the lock excludes nobody.
_CHAIN_LOCK_KEY = 0x0C_FA_C7_06


def _serialise_sqlite_appends(engine: Engine) -> None:
    """Make every transaction on ``engine`` start with ``BEGIN IMMEDIATE``.

    pysqlite does not open a transaction before a SELECT — only before DML —
    so out of the box :meth:`AuditStore.record` reads the chain tail OUTSIDE
    the transaction it then inserts in. Two overlapping appends read the same
    predecessor, both insert, and neither errors: the chain forks silently
    (#306, observed live on 2026-07-30). No lock in ``record`` can fix that,
    because by the time the INSERT takes SQLite's write lock the stale read has
    already happened.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so the tail read and the
    insert are one critical section against SQLite's single writer. This is
    SQLAlchemy's documented recipe for real serialisable behaviour on pysqlite:
    turn off the driver's implicit transaction handling and emit BEGIN here.

    Scoped to the audit store's own engine on purpose — the cockpit's other
    stores share the database file and must not be serialised behind it.
    """

    @event.listens_for(engine, "connect")
    def _disable_implicit_begin(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def _lock_chain(session: Session) -> None:
    """Hold the chain against concurrent appends for the rest of the transaction.

    SQLite needs nothing here — :func:`_serialise_sqlite_appends` already took
    the write lock when the transaction opened.

    PostgreSQL needs an explicit lock, and it must be one that covers a row
    that does not exist yet. ``SELECT ... FOR UPDATE`` on the tail row does NOT:
    under READ COMMITTED both appenders see the same tail, both lock it, and the
    second still inserts a sibling once the first commits — a phantom the row
    lock cannot see. A transaction-scoped advisory lock has no such gap and is
    released on commit or rollback either way.
    """
    # ponytail: one lock for the whole chain. Appends are single rows a few
    # times a second; finer granularity would need a chain id to lock on.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK_KEY})


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
        """The chained fields, taken RAW off the row.

        Deliberately not passed through :func:`as_utc`: the canonical form must
        stay byte-identical to what was hashed at write time. It already is
        invariant to tz-awareness (see :func:`_canonical_ts`), so this is belt
        and braces rather than a live constraint — but the read path (#258)
        must never be able to reach the hash input.
        """
        return {name: getattr(self, name) for name in _CANONICAL_FIELDS}

    def to_model(self) -> AuditEntryModel:
        return AuditEntryModel(
            id=self.id,
            # Labelled UTC on the way out only (#258). The stored value is
            # unchanged, so `verify()` still recomputes the original hash.
            ts=as_utc(self.ts),
            actor=redact_actor(self.actor),
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


def _short(digest: str | None) -> str:
    """A hash prefix, for anomaly text that a human has to read."""
    return "the genesis marker" if digest is None else f"{digest[:10]}..."


class ChainBreak(BaseModel):
    """One anomaly found by :meth:`AuditStore.check`.

    ``kind`` is one of ``mutated``, ``duplicate``, ``forked``, ``dangling`` —
    see :meth:`AuditStore.check` for what each one means and which of them are
    tamper evidence.
    """

    id: int
    kind: str
    detail: str


# `AuditStore.list` shadows the builtin inside the class body, so a return type
# written there as `list[...]` resolves to the method and fails --strict. These
# aliases are the same types spelled where the builtin is still the builtin.
ChainBreaks = list[ChainBreak]
EntryModels = list[AuditEntryModel]
EntryIds = list[int]

# Verdicts, worst first. Strings rather than an enum because they cross into the
# frontend, which must render a value it has never heard of as itself rather
# than dropping the row (#431).
TAMPERED = "tampered"
FORKED = "forked"
OK = "ok"

# How long a computed :class:`ChainReport` is served before the table is walked
# again. Short enough that a real finding surfaces on the operator's next look,
# long enough that the endpoint cannot be turned into a full-table scan per
# request.
_REPORT_TTL = timedelta(seconds=30)


class ChainReport(BaseModel):
    """The whole tamper-evidence verdict, in the shape an operator reads (#309).

    ``findings`` is every anomaly that still wants an answer; ``acknowledged_forks``
    is the ones that already have one. ``rows`` and ``checked_at`` are there so a
    reader can tell "the chain is sound" from "the check did not really run" —
    the distinction #306 lacked.
    """

    verdict: str
    rows: int
    checked_at: datetime
    findings: ChainBreaks
    acknowledged_forks: EntryIds


def parse_acknowledged_forks(raw: str | None) -> frozenset[int]:
    """Parse ``CFACTORY_AUDIT_ACKNOWLEDGED_FORKS`` — ids, comma separated.

    An acknowledgement is a DECLARATION, not a repair. The live cockpit's chain
    forked once at entry 2178 under the pre-#310 write race; every HMAC involved
    is valid and rewriting the row to relink it is precisely the operation the
    chain exists to detect. So the row stays exactly as written and the deploy
    states, in reviewable configuration, which specific ids it has already
    explained. Anything else stays loud.

    Empty (the default) acknowledges nothing, which is the right posture for a
    fresh database: there, any fork at all is news.

    Ids are matched exactly rather than as a "everything below N" watermark, so
    pointing a deployment at a different database cannot silently absolve a
    different row that happens to carry a lower id.
    """
    if not raw:
        return frozenset()
    return frozenset(int(part) for part in raw.replace(",", " ").split() if part)


def _classify(
    row: AuditEntry,
    recomputed: str,
    expected_prev: str | None,
    by_hash: dict[str, list[int]],
    by_parent: dict[str | None, list[int]],
) -> ChainBreak | None:
    """Name the anomaly this row carries, or ``None`` if it is sound.

    See :meth:`AuditStore.check` for what each kind means. The order matters: a
    row that fails its own HMAC is a mutation whatever its links say, and a
    replayed row shares a parent exactly the way a raced append does, so both
    are decided before the link is looked at.
    """
    if row.entry_hash != recomputed:
        return ChainBreak(
            id=row.id, kind="mutated", detail="entry_hash is not the HMAC of this row's fields"
        )
    twins = [i for i in by_hash[row.entry_hash] if i != row.id]
    if twins:
        return ChainBreak(id=row.id, kind="duplicate", detail=f"entry_hash is shared with {twins}")
    if row.prev_hash == expected_prev:
        return None
    siblings = [i for i in by_parent[row.prev_hash] if i != row.id]
    parent_exists = row.prev_hash is None or row.prev_hash in by_hash
    if siblings and parent_exists:
        return ChainBreak(
            id=row.id,
            kind=FORKED,
            detail=(
                f"shares parent {_short(row.prev_hash)} with {siblings}; every HMAC "
                "involved is valid, so this is a concurrent append (#306)"
            ),
        )
    return ChainBreak(
        id=row.id,
        kind="dangling",
        detail=f"prev_hash {_short(row.prev_hash)} is not the preceding entry",
    )


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
        acknowledged_forks: Iterable[int] | None = None,
    ) -> None:
        self._engine = make_engine(url)
        if self._engine.dialect.name == "sqlite":
            _serialise_sqlite_appends(self._engine)
        self._session = sessionmaker(self._engine, expire_on_commit=False)
        self._secret = hmac_secret if hmac_secret is not None else get_settings().audit_hmac_secret
        self._acknowledged = (
            frozenset(acknowledged_forks)
            if acknowledged_forks is not None
            else parse_acknowledged_forks(get_settings().audit_acknowledged_forks)
        )
        self._report: ChainReport | None = None
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
        """Append a new audit entry, chaining its hash to the previous entry.

        The tail read and the insert are one critical section (:func:`_lock_chain`);
        without that, concurrent appends fork the chain (#306).
        """
        with self._session.begin() as session:
            _lock_chain(session)
            prev_hash = session.scalars(
                select(AuditEntry.entry_hash).order_by(AuditEntry.id.desc()).limit(1)
            ).first()
            row = AuditEntry(
                ts=_now(),
                actor=redact_actor(actor),
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

    def list(self, limit: int = 100) -> EntryModels:
        """Return the most recent entries, newest first."""
        with self._session() as session:
            rows = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit))
            return [r.to_model() for r in rows]

    def check(self) -> ChainBreaks:
        """Recompute the chain and classify every anomaly found.

        ``verify`` used to answer only "which ids do not line up", which cannot
        tell a forged row from a benign write race — and on the live cockpit the
        standing fork made it answer "tampered" every time, so it stopped
        carrying information (#306). This is the diagnosis; :meth:`verify` is the
        alarm built on top of it.

        Kinds, in the order they are decided:

        ``mutated``
            The row's own ``entry_hash`` is not the HMAC of its own fields. Some
            field was edited, or the hash was forged. Tamper evidence.
        ``duplicate``
            Another row carries the same ``entry_hash``. Entry hashes cover a
            microsecond timestamp, so a repeat means a row was copied — a replay.
            Tamper evidence.
        ``forked``
            The row's own HMAC is valid, its ``prev_hash`` is not its
            predecessor's hash, but a sibling row chains to that same parent and
            the parent exists. That is the signature of two appends racing on the
            tail read, and nothing else: a forged branch needs the HMAC secret,
            and with the secret an attacker would not need to fork.
        ``dangling``
            The link is wrong and no sibling shares the parent — a deleted or
            reordered entry. Tamper evidence.

        Rows are walked by id; a break does not cascade into the rows after it.
        """
        return self._scan()[1]

    def _scan(self) -> tuple[int, ChainBreaks]:
        """One pass over the table: how many rows, and every anomaly in them.

        :meth:`check` and :meth:`report` both need the row count and the
        classification, and the count must be the count of what was actually
        walked — a second ``COUNT(*)`` could disagree with it after a concurrent
        append and quietly make the report describe a table it never read.
        """
        with self._session() as session:
            rows = list(session.scalars(select(AuditEntry).order_by(AuditEntry.id.asc())))

        by_hash: dict[str, list[int]] = {}
        by_parent: dict[str | None, list[int]] = {}
        for row in rows:
            by_hash.setdefault(row.entry_hash, []).append(row.id)
            by_parent.setdefault(row.prev_hash, []).append(row.id)

        found: list[ChainBreak] = []
        expected_prev: str | None = None
        for row in rows:
            recomputed = compute_entry_hash(self._secret, row._hashed_values(), row.prev_hash)
            anomaly = _classify(row, recomputed, expected_prev, by_hash, by_parent)
            if anomaly is not None:
                found.append(anomaly)
            expected_prev = row.entry_hash
        return len(rows), found

    def report(self) -> ChainReport:
        """The operator-facing verdict: what the chain says about itself (#309).

        Three verdicts, and the middle one is the whole point:

        ``tampered``
            A ``mutated``, ``duplicate`` or ``dangling`` entry — the kinds
            :meth:`verify` has always reported. Something happened to the record.
        ``forked``
            A concurrent-append fork that this deployment has NOT acknowledged.
            Not tamper evidence, but after #310 it should be impossible, so an
            unexplained one means the serialisation regressed and wants a human.
        ``ok``
            No anomalies, or only forks that configuration already explains.

        That middle rung is what stops this surface repeating #306's own defect.
        The live chain carries a permanent fork at entry 2178 and always will,
        because relinking it is indistinguishable from tampering. Colouring that
        red forever would produce an alarm that is always on — which is exactly
        how the real alarm went a week unread. So the known fork is declared
        (:func:`parse_acknowledged_forks`), counted, shown, and does not raise
        the verdict; anything undeclared still does.

        Acknowledgement only ever forgives a ``forked`` classification. If entry
        2178 were later mutated it would classify as ``mutated``, which no
        configuration suppresses — so listing an id here cannot be used to hide a
        later edit to that same row.

        No HMAC secret, and no field of any entry, appears in the result: it
        carries counts, ids, kinds, and hash PREFIXES for the human reading it.
        """
        cached = self._report
        # ponytail: recompute at most every 30s. The scan is O(rows) and takes no
        # lock, and this is a READ-scoped endpoint the Audit view polls — a cache
        # is the whole rate limit. If the trail outgrows a sub-second scan, move
        # the computation to a schedule and serve the last result.
        if cached is not None and _now() - cached.checked_at < _REPORT_TTL:
            return cached
        rows, breaks = self._scan()
        acknowledged = sorted(
            b.id for b in breaks if b.kind == FORKED and b.id in self._acknowledged
        )
        findings = [b for b in breaks if not (b.kind == FORKED and b.id in self._acknowledged)]
        if any(b.kind != FORKED for b in findings):
            verdict = TAMPERED
        elif findings:
            verdict = FORKED
        else:
            verdict = OK
        report = ChainReport(
            verdict=verdict,
            rows=rows,
            checked_at=_now(),
            findings=findings,
            acknowledged_forks=acknowledged,
        )
        self._report = report
        return report

    def verify(self) -> EntryIds:
        """Return the ids of entries carrying tamper evidence — empty means none.

        Mutation, replay and deletion (see :meth:`check`). A ``forked`` entry is
        deliberately NOT here: it is a write race this store used to permit
        (#306), every field of it still hashes, and leaving it in made the alarm
        permanently on and therefore unreadable. Forks are still reported — by
        :meth:`check`, which is where an operator or auditor sees the whole
        picture. Nothing is hidden and no row is rewritten to make this empty.
        """
        return [b.id for b in self.check() if b.kind != FORKED]


_audit_store: AuditStore | None = None


def get_audit_store(settings: Settings | None = None) -> AuditStore:
    """Cached default audit store, built from settings (lazy)."""
    global _audit_store
    if _audit_store is None:
        settings = settings or get_settings()
        _audit_store = AuditStore(
            settings.database_url,
            hmac_secret=settings.audit_hmac_secret,
            acknowledged_forks=parse_acknowledged_forks(settings.audit_acknowledged_forks),
        )
    return _audit_store


def reset_audit_store() -> None:
    """Drop the cached audit store (tests)."""
    global _audit_store
    _audit_store = None

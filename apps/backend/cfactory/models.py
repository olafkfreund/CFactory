"""Core domain models.

The WorkItem is CFactory's linchpin: it threads one unit of work across the
three services, keyed by the GitHub issue number (synthetic fallback otherwise).

These are skeleton shapes for #5. Persistence + the full field set land in #6
(WorkItem correlation model + Postgres/Alembic).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The base token-usage shape (RFC-0001 v1.1 `usage` block) is the Factory hub's
# GENERATED single source of truth, vendored + pinned at
# cfactory._contracts.factory_contracts (Factory#160). CFactory's TokenUsage
# DERIVES from it and adds the v1.3 breakdowns below, so the shared scalar
# contract (input/output/total tokens + cost + model) is defined once fleet-wide
# instead of re-declared per service. The JSON shape is unchanged (see the
# behaviour tests in tests/test_factory_contracts_consumption.py).
from ._contracts.factory_contracts import Usage as _SharedUsage


class Service(str, Enum):
    PFACTORY = "pfactory"
    AIFACTORY = "aifactory"
    TFACTORY = "tfactory"


class Stage(str, Enum):
    PLAN = "plan"
    CODE = "code"
    TEST = "test"


class BudgetInfo(BaseModel):
    """Soft-budget envelope (RFC-0001 v1.3 P2 — *observe + warn, never abort*).

    Carried on a terminal event's ``usage`` ONLY when the contract set a budget;
    absent on the common (no-budget) case, where everything behaves exactly as
    before. Purely informational: CFactory surfaces an "over budget" badge when
    ``exceeded`` is true — no behaviour changes when it's absent or false.
    """

    limit_usd: float = 0.0
    spent_usd: float = 0.0
    exceeded: bool = False


class RoutingInfo(BaseModel):
    """Pre-execution cost-aware routing decision (RFC-0014 ``execution.routing``).

    Emitted by PFactory on the plan/contract event: the deterministic router's
    forward-looking pick — the routing ``class`` (``economy``/``standard``/
    ``premium``/``governed``), the cost ``estimate`` (``cost_estimate_usd``,
    ``None`` for subscription/local where there is no metered ``$``, mirroring
    CFactory's billing-mode display), the ``ceiling``, the per-role
    ``phase_models``, the chosen ``runtime``, the ``budget_mode``
    (``observe``/``enforce``), the ``autonomy`` verdict + reason (§4b), and the
    human-readable ``rationale``. Purely informational in the cockpit — CFactory
    surfaces estimate-vs-actual and the rationale; it never changes a run.

    Every field is optional so a partial routing block (or a legacy event with
    none) round-trips unchanged; ``extra="ignore"`` keeps unknown wire fields
    from breaking ingest.
    """

    model_config = ConfigDict(extra="ignore")

    routing_class: str | None = None
    cost_estimate_usd: float | None = None
    cost_ceiling_usd: float | None = None
    budget_mode: str | None = None
    runtime: str | None = None
    policy: str | None = None
    rationale: str | None = None
    autonomy: str | None = None
    autonomy_reason: str | None = None
    # Per-role model picks: {"planning": "opus", "coding": "sonnet", ...}.
    phase_models: dict[str, str] = Field(default_factory=dict)


class TokenUsage(_SharedUsage):
    """LLM token/cost usage for one stage (RFC-0001 v1.1 additive `usage` block).

    Derives from the hub's generated ``Usage`` (vendored single source of truth,
    Factory#160): the scalar contract — ``input_tokens`` / ``output_tokens`` /
    ``total_tokens`` / ``cost_usd`` / ``model`` — is inherited, and CFactory adds
    the v1.3 breakdowns below. The scalar defaults are pinned to ``0`` here
    (CFactory aggregates these in arithmetic, so a missing block reads as zero,
    not ``None``) — this preserves the exact prior wire/JSON shape; the shared
    base only supplies the field *contract*, not the defaults.

    Optional everywhere — only present when a service instruments and emits it
    (AIFactory does today; PFactory/TFactory pending). Aggregated by CFactory.

    RFC-0001 v1.3 adds additive per-worker/per-provider/per-model breakdowns
    (``workers`` / ``by_provider`` / ``by_model``) carried on the *terminal*
    event for a parallel run. The scalar aggregate fields above are KEPT — old
    consumers ignore the new fields. ``workers`` is a list here (the wire shape);
    CFactory keys it by ``worker_id`` on the service slice (see ``ServiceState``).

    ``budget`` is the optional soft-budget block (RFC-0001 v1.3 P2): present only
    when the contract set a budget, absent (``None``) on the common case.
    """

    # Keep CFactory's historic extra-handling (ignore unknown wire fields) rather
    # than inheriting the shared base's ``extra="allow"``; the JSON shape stays
    # byte-for-byte what it was before this derivation.
    model_config = ConfigDict(extra="ignore")

    # Re-declared to pin the zero defaults (the shared base defaults to None).
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    # v1.3 additive breakdowns (terminal event only; absent on legacy events).
    workers: list[WorkerUsage] | None = None
    by_provider: dict[str, dict[str, Any]] | None = None
    by_model: dict[str, dict[str, Any]] | None = None
    # v1.3 P2 soft budget (terminal event only; None when no budget was set).
    budget: BudgetInfo | None = None


class ProgressPoint(BaseModel):
    """One per-worker heartbeat sample (RFC-0001 v1.3, ``phase:"worker_progress"``).

    A throttled in-flight sample emitted ~every 10s WHILE a worker runs (Tier
    1.5 — the ticking chain). Unlike the terminal ``phase:"worker"`` record, a
    progress point is a STREAM sample: it is appended to a rolling per-worker
    series and never mutates the service slice's status/phase/usage. ``ts`` is
    the worker-reported ``updated_at`` (epoch ms preferred; CFactory stamps it
    from the event's ``updated_at`` so the series is monotone for the sparkline).
    """

    ts: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0


class WorkerUsage(BaseModel):
    """Per-worker (per-subtask) usage record (RFC-0001 v1.3, ``phase:"worker"``).

    Produced where the work happens — one per parallel coding worker / subtask.
    Local providers (Ollama) report ``cost_usd: 0`` but still carry tokens +
    duration. Keyed by ``worker_id`` on the service slice; re-emit replaces (no
    double count). Dedup key for the live sub-event is
    ``(service, correlation_key, worker_id)``.
    """

    worker_id: str
    subtask_id: str | None = None
    agent_phase: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    # Billing mode of this worker's provider (#96): api/cloud are metered (show
    # cost); subscription/local are not (show tokens + time). Carried on the
    # terminal by_provider rollup today; optional on the live sub-event.
    billing_mode: str | None = None
    # Heartbeat (``phase:"worker_progress"``) samples carry elapsed-so-far as
    # ``elapsed_ms`` (the terminal ``phase:"worker"`` record uses ``duration_ms``).
    # Optional so the terminal worker record round-trips unchanged.
    elapsed_ms: int = 0


class CompletionEvent(BaseModel):
    """Normalized completion envelope emitted by the three services (see Factory#4).

    Standard schema all services conform to; consumed by the webhook ingress (#11).
    The optional ``usage`` block (RFC-0001 v1.1) carries per-stage token/cost.

    ``id`` is the per-event idempotency key from the additive envelope upgrade
    (AIFactory #466 / TFactory #282). When present the store dedups on it —
    exactly-once per event — which both makes the outbox relay's re-delivery a
    no-op and lets a legitimate re-run after handback (same service+status, new
    ``id``) through, where the old ``(service, status)`` key wrongly collided.
    Optional so legacy producers that don't emit it still ingest unchanged.
    """

    id: str | None = None
    correlation_key: str
    service: Service
    task_id: str
    status: str
    phase: str | None = None
    # #471 cutover: CloudEvents ``time`` is the canonical event timestamp; the
    # legacy ``updated_at`` is being retired from producers. Accept either —
    # ``_reconcile_timestamps`` backfills both so existing ``updated_at`` readers
    # and new ``time`` readers keep working through the cutover.
    time: datetime | None = None
    updated_at: datetime | None = None
    usage: TokenUsage | None = None
    # v1.3 live per-worker sub-event. The SAME ``worker`` payload carries two
    # event kinds, distinguished by ``phase``:
    #  - ``phase:"worker"``        terminal per-worker record → upserts the
    #                              slice's ``workers`` map (keyed by worker_id).
    #  - ``phase:"worker_progress"`` Tier-1.5 heartbeat sample → appended to the
    #                              rolling ``worker_progress`` series (the ~10s
    #                              ticking stream). Reads total_tokens/cost_usd/
    #                              elapsed_ms; never touches the scalar slice.
    # Both leave the service-level ``usage``/status/phase untouched. Absent on
    # every legacy / non-worker event.
    worker: WorkerUsage | None = None
    # RFC-0007 (#88): honest access annotation a service may attach when a
    # credentialed (VAL-3) lane could not run (e.g. TFactory when the contract's
    # access wasn't curated/reachable): {val3, reason, risk, blocked}. Optional —
    # absent on every event that declares no external access.
    access: dict[str, Any] | None = None
    # RFC-0006 (#76): gate-normalized verification block a verifier attaches —
    # {target_level, achieved_level, levels[], claim, _gate}. The cockpit renders
    # achieved_level + the honest claim so a VAL-2 result never looks like "done".
    verification: dict[str, Any] | None = None
    # RFC-0014 (#124): pre-execution cost-aware routing decision PFactory attaches
    # to the plan/contract event (routing class, cost estimate vs ceiling,
    # per-role models, runtime, budget_mode, autonomy verdict, rationale). The
    # cockpit shows it next to actual rolled-up spend. Optional — absent on every
    # event that carries no routing block, so legacy events ingest unchanged.
    routing: RoutingInfo | None = None

    @model_validator(mode="after")
    def _reconcile_timestamps(self) -> CompletionEvent:
        """Keep CloudEvents ``time`` and legacy ``updated_at`` in sync (#471).

        Producers are migrating from ``updated_at`` to CloudEvents ``time``. Accept
        whichever is present and backfill the other so every existing
        ``event.updated_at`` reader and any new ``event.time`` reader both work. If
        neither is present (malformed event) default to now so ingestion never
        crashes on a timestamp.
        """
        if self.updated_at is None:
            self.updated_at = self.time or datetime.now(UTC)
        if self.time is None:
            self.time = self.updated_at
        return self


class ServiceState(BaseModel):
    """Per-service slice of a WorkItem's state.

    The scalar ``usage`` slice is unchanged. v1.3 adds an additive per-worker
    view: ``workers`` maps ``worker_id -> WorkerUsage`` (live sub-events upsert
    here, idempotent by ``worker_id``), and ``by_provider`` / ``by_model`` hold
    rollups (recomputed from ``workers`` for the API, or stored straight from a
    terminal event's ``usage`` breakdown). All default empty so legacy slices
    round-trip unchanged.

    Tier 1.5 adds ``worker_progress``: a rolling per-worker SERIES keyed by
    ``worker_id -> list[ProgressPoint]`` fed by live ``phase:"worker_progress"``
    heartbeats (the ~10s ticking stream). It is capped per worker to bound store
    growth, never mutates the scalar slice, and is PRUNED on a terminal event for
    the task (only needed while running). Defaults empty so legacy slices and the
    terminal-only ``workers`` view round-trip unchanged.
    """

    task_id: str | None = None
    status: str | None = None
    phase: str | None = None
    repo: str | None = None  # target repo owner/name (W5, Factory #218)
    usage: TokenUsage | None = None
    workers: dict[str, WorkerUsage] = Field(default_factory=dict)
    worker_progress: dict[str, list[ProgressPoint]] = Field(default_factory=dict)
    by_provider: dict[str, dict[str, Any]] = Field(default_factory=dict)
    by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


class Liveness(BaseModel):
    """Request-time liveness signal for a WorkItem (RFC-0008 §3.4, #105).

    A task that hangs in a non-terminal stage (e.g. TFactory stuck at
    ``reviewing``) produced no signal in the watch plane. ``stalled`` flags a
    work item whose furthest-along stage is still active but hasn't changed
    within ``deadline_seconds`` — surfaced as an alert, not a quiet phase.
    """

    last_activity_age_seconds: float = 0.0
    active_service: str | None = None  # furthest-along service still non-terminal
    active_status: str | None = None
    stalled: bool = False
    deadline_seconds: float = 0.0


class WorkItem(BaseModel):
    """A unit of work threaded across plan -> code -> test."""

    correlation_key: str
    title: str | None = None
    pfactory: ServiceState = Field(default_factory=ServiceState)
    aifactory: ServiceState = Field(default_factory=ServiceState)
    tfactory: ServiceState = Field(default_factory=ServiceState)
    timeline: list[CompletionEvent] = Field(default_factory=list)
    # Persistence timestamps (#105): surfaced so the cockpit can show a
    # last-activity age and compute staleness live. None for in-memory items.
    created_at: datetime | None = None
    updated_at: datetime | None = None


# TokenUsage / CompletionEvent reference WorkerUsage as a forward ref (it is
# defined after TokenUsage to keep the file readable). Resolve them now.
TokenUsage.model_rebuild()
CompletionEvent.model_rebuild()

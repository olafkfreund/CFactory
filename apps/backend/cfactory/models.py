"""Core domain models.

The WorkItem is CFactory's linchpin: it threads one unit of work across the
three services, keyed by the GitHub issue number (synthetic fallback otherwise).

These are skeleton shapes for #5. Persistence + the full field set land in #6
(WorkItem correlation model + Postgres/Alembic).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Service(str, Enum):
    PFACTORY = "pfactory"
    AIFACTORY = "aifactory"
    TFACTORY = "tfactory"


class Stage(str, Enum):
    PLAN = "plan"
    CODE = "code"
    TEST = "test"


class TokenUsage(BaseModel):
    """LLM token/cost usage for one stage (RFC-0001 v1.1 additive `usage` block).

    Optional everywhere — only present when a service instruments and emits it
    (AIFactory does today; PFactory/TFactory pending). Aggregated by CFactory.

    RFC-0001 v1.3 adds additive per-worker/per-provider/per-model breakdowns
    (``workers`` / ``by_provider`` / ``by_model``) carried on the *terminal*
    event for a parallel run. The scalar aggregate fields above are KEPT — old
    consumers ignore the new fields. ``workers`` is a list here (the wire shape);
    CFactory keys it by ``worker_id`` on the service slice (see ``ServiceState``).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    # v1.3 additive breakdowns (terminal event only; absent on legacy events).
    workers: list[WorkerUsage] | None = None
    by_provider: dict[str, dict[str, Any]] | None = None
    by_model: dict[str, dict[str, Any]] | None = None


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
    updated_at: datetime
    usage: TokenUsage | None = None
    # v1.3 live per-worker sub-event (``phase:"worker"``). Carries one worker's
    # record; ingested into the service slice's ``workers`` map keyed by
    # ``worker_id``, leaving the service-level ``usage`` slice untouched. Absent
    # on every legacy / non-worker event.
    worker: WorkerUsage | None = None


class ServiceState(BaseModel):
    """Per-service slice of a WorkItem's state.

    The scalar ``usage`` slice is unchanged. v1.3 adds an additive per-worker
    view: ``workers`` maps ``worker_id -> WorkerUsage`` (live sub-events upsert
    here, idempotent by ``worker_id``), and ``by_provider`` / ``by_model`` hold
    rollups (recomputed from ``workers`` for the API, or stored straight from a
    terminal event's ``usage`` breakdown). All default empty so legacy slices
    round-trip unchanged.
    """

    task_id: str | None = None
    status: str | None = None
    phase: str | None = None
    usage: TokenUsage | None = None
    workers: dict[str, WorkerUsage] = Field(default_factory=dict)
    by_provider: dict[str, dict[str, Any]] = Field(default_factory=dict)
    by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


class WorkItem(BaseModel):
    """A unit of work threaded across plan -> code -> test."""

    correlation_key: str
    title: str | None = None
    pfactory: ServiceState = Field(default_factory=ServiceState)
    aifactory: ServiceState = Field(default_factory=ServiceState)
    tfactory: ServiceState = Field(default_factory=ServiceState)
    timeline: list[CompletionEvent] = Field(default_factory=list)


# TokenUsage / CompletionEvent reference WorkerUsage as a forward ref (it is
# defined after TokenUsage to keep the file readable). Resolve them now.
TokenUsage.model_rebuild()
CompletionEvent.model_rebuild()

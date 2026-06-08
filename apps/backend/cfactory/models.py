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
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None


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


class ServiceState(BaseModel):
    """Per-service slice of a WorkItem's state."""

    task_id: str | None = None
    status: str | None = None
    phase: str | None = None
    usage: TokenUsage | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class WorkItem(BaseModel):
    """A unit of work threaded across plan -> code -> test."""

    correlation_key: str
    title: str | None = None
    pfactory: ServiceState = Field(default_factory=ServiceState)
    aifactory: ServiceState = Field(default_factory=ServiceState)
    tfactory: ServiceState = Field(default_factory=ServiceState)
    timeline: list[CompletionEvent] = Field(default_factory=list)

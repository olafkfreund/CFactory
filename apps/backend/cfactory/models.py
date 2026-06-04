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


class CompletionEvent(BaseModel):
    """Normalized completion envelope emitted by the three services (see Factory#4).

    Standard schema all services conform to; consumed by the webhook ingress (#11).
    """

    correlation_key: str
    service: Service
    task_id: str
    status: str
    phase: str | None = None
    updated_at: datetime


class ServiceState(BaseModel):
    """Per-service slice of a WorkItem's state."""

    task_id: str | None = None
    status: str | None = None
    phase: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class WorkItem(BaseModel):
    """A unit of work threaded across plan -> code -> test."""

    correlation_key: str
    title: str | None = None
    pfactory: ServiceState = Field(default_factory=ServiceState)
    aifactory: ServiceState = Field(default_factory=ServiceState)
    tfactory: ServiceState = Field(default_factory=ServiceState)
    timeline: list[CompletionEvent] = Field(default_factory=list)

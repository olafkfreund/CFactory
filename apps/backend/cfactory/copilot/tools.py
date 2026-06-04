"""Copilot read tools (#14).

Pure, structured queries over the WorkItem store — used to enrich the copilot's
context and exposed as read API endpoints for the cockpit UI. Cost roll-ups are
intentionally omitted: the WorkItem/CompletionEvent model does not yet carry
cost data (a future enhancement). Latency is derived from event timestamps.
"""

from __future__ import annotations

from collections import Counter

from ..models import Stage, WorkItem
from ..store import WorkItemStore

_STAGE_ATTR = {Stage.PLAN: "pfactory", Stage.CODE: "aifactory", Stage.TEST: "tfactory"}


def _slice(wi: WorkItem, stage: Stage):
    return getattr(wi, _STAGE_ATTR[stage])


def query_work_items(
    store: WorkItemStore, *, stage: Stage | None = None, status: str | None = None
) -> list[WorkItem]:
    """Filter work items by stage activity and/or status (any slice)."""
    items = store.list()
    out = []
    for wi in items:
        if stage is not None and not _slice(wi, stage).status:
            continue
        if status is not None and status not in {
            wi.pfactory.status, wi.aifactory.status, wi.tfactory.status
        }:
            continue
        out.append(wi)
    return out


def summarize_timeline(store: WorkItemStore, correlation_key: str) -> dict | None:
    """Ordered event timeline for one work item, with total span in seconds."""
    wi = store.get(correlation_key)
    if wi is None:
        return None
    events = [
        {"service": e.service.value, "status": e.status, "phase": e.phase,
         "updated_at": e.updated_at.isoformat()}
        for e in wi.timeline
    ]
    span = None
    if len(wi.timeline) >= 2:
        span = (wi.timeline[-1].updated_at - wi.timeline[0].updated_at).total_seconds()
    return {
        "correlation_key": wi.correlation_key,
        "title": wi.title,
        "event_count": len(events),
        "events": events,
        "span_seconds": span,
    }


def rollups(store: WorkItemStore) -> dict:
    """Aggregate counts + latency across the board (cost not tracked yet)."""
    items = store.list()
    by_stage = {"plan": 0, "code": 0, "test": 0}
    by_status: Counter[str] = Counter()
    spans: list[float] = []
    total_events = 0
    for wi in items:
        if wi.pfactory.status:
            by_stage["plan"] += 1
        if wi.aifactory.status:
            by_stage["code"] += 1
        if wi.tfactory.status:
            by_stage["test"] += 1
        for s in (wi.pfactory, wi.aifactory, wi.tfactory):
            if s.status:
                by_status[s.status] += 1
        total_events += len(wi.timeline)
        if len(wi.timeline) >= 2:
            spans.append((wi.timeline[-1].updated_at - wi.timeline[0].updated_at).total_seconds())
    latency = None
    if spans:
        latency = {"avg_seconds": sum(spans) / len(spans), "max_seconds": max(spans)}
    return {
        "total_work_items": len(items),
        "by_stage": by_stage,
        "by_status": dict(by_status),
        "total_events": total_events,
        "latency": latency,
        "cost": None,  # not tracked yet
    }


def rollups_summary_line(store: WorkItemStore) -> str:
    """One-line rollups summary for the copilot context."""
    r = rollups(store)
    bs = r["by_stage"]
    return (
        f"Board: {r['total_work_items']} work items "
        f"(plan={bs['plan']}, code={bs['code']}, test={bs['test']}), "
        f"{r['total_events']} events."
    )

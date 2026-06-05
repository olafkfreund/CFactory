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
    cost = 0.0
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
            if s.usage:
                cost += s.usage.cost_usd
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
        "cost": cost if cost else None,  # real once a service emits RFC-0001 usage
    }


def token_totals(store: WorkItemStore) -> dict:
    """Aggregate token/cost usage from the RFC-0001 `usage` block (#token-spine).

    Returns total + by_service (with an `instrumented` flag so the UI can show
    'not instrumented yet' honestly) + by_work_item. `by_project` is deferred —
    CFactory has no project dimension on the WorkItem yet.
    """
    services = ("pfactory", "aifactory", "tfactory")
    keys = ("input_tokens", "output_tokens", "total_tokens")
    by_service = {
        s: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
            "instrumented": False}
        for s in services
    }
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    by_work_item: list[dict] = []

    for wi in store.list():
        wi_tot = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
        for svc in services:
            u = getattr(wi, svc).usage
            if u is None:
                continue
            b = by_service[svc]
            b["instrumented"] = True
            for k in keys:
                v = getattr(u, k)
                b[k] += v
                wi_tot[k] += v
                total[k] += v
            b["cost_usd"] += u.cost_usd
            wi_tot["cost_usd"] += u.cost_usd
            total["cost_usd"] += u.cost_usd
        if wi_tot["total_tokens"] or wi_tot["cost_usd"]:
            by_work_item.append({"correlation_key": wi.correlation_key, "title": wi.title, **wi_tot})

    by_work_item.sort(key=lambda w: w["total_tokens"], reverse=True)
    return {"total": total, "by_service": by_service, "by_work_item": by_work_item}


def rollups_summary_line(store: WorkItemStore) -> str:
    """One-line rollups summary for the copilot context."""
    r = rollups(store)
    bs = r["by_stage"]
    return (
        f"Board: {r['total_work_items']} work items "
        f"(plan={bs['plan']}, code={bs['code']}, test={bs['test']}), "
        f"{r['total_events']} events."
    )

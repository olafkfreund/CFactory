"""Copilot read tools (#14).

Pure, structured queries over the WorkItem store — used to enrich the copilot's
context and exposed as read API endpoints for the cockpit UI. Roll-ups aggregate
the RFC-0001 ``usage`` block carried on each service slice: token totals, cost
(real dollars for metered work), and latency derived from event timestamps.
"""

from __future__ import annotations

from collections import Counter

from cfactory.usage import add_usage, empty_bucket

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
            wi.pfactory.status,
            wi.aifactory.status,
            wi.tfactory.status,
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
        {
            "service": e.service.value,
            "status": e.status,
            "phase": e.phase,
            "updated_at": e.updated_at.isoformat(),
        }
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
    """Aggregate counts + latency + cost across the board (RFC-0001 usage)."""
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


_METERED_MODES = {"api", "cloud"}


def _wi_elapsed_seconds(wi) -> int | None:
    """Wall time for a work item: span of its completion-event timeline. The
    honest 'time spent' headline — present even when per-worker durations aren't
    (#96). ``None`` when the timeline is too thin to measure."""
    times = [e.updated_at for e in (wi.timeline or []) if getattr(e, "updated_at", None)]
    if len(times) < 1:
        return None
    return max(0, int((max(times) - min(times)).total_seconds()))


def _billing_summary(wi) -> dict | None:
    """Bucket a work item's usage by billing mode so the cockpit shows the right
    metric (#96): real dollars only for metered (api/cloud) work, tokens + time
    for subscription/local. Sourced from each service's ``by_provider`` rollup
    (which carries ``billing_mode``). ``None`` when no provider breakdown exists
    yet (older events) — the cockpit then falls back to the legacy cost display."""
    by_mode: dict[str, dict] = {}
    for svc in ("pfactory", "aifactory", "tfactory"):
        bp = getattr(getattr(wi, svc), "by_provider", None)
        for vals in (bp or {}).values():
            if not isinstance(vals, dict):
                continue
            mode = vals.get("billing_mode") or "unknown"
            slot = by_mode.setdefault(mode, {"total_tokens": 0, "cost_usd": 0.0, "duration_ms": 0})
            slot["total_tokens"] += int(vals.get("total_tokens", 0) or 0)
            slot["cost_usd"] = round(slot["cost_usd"] + float(vals.get("cost_usd", 0.0) or 0.0), 6)
            slot["duration_ms"] += int(vals.get("duration_ms", 0) or 0)
    if not by_mode:
        return None
    metered_cost = round(sum(v["cost_usd"] for m, v in by_mode.items() if m in _METERED_MODES), 6)
    nonmetered_tokens = sum(
        v["total_tokens"] for m, v in by_mode.items() if m not in _METERED_MODES
    )
    return {
        "modes": sorted(by_mode.keys()),
        "by_mode": by_mode,
        "metered_cost_usd": metered_cost,
        "nonmetered_tokens": nonmetered_tokens,
        # True when any real-dollar work happened → the cockpit may show cost.
        "has_metered": any(m in _METERED_MODES for m in by_mode),
    }


def token_totals(store: WorkItemStore) -> dict:
    """Aggregate token/cost usage from the RFC-0001 `usage` block (#token-spine).

    Returns total + by_service (with an `instrumented` flag so the UI can show
    'not instrumented yet' honestly) + by_work_item. Each row carries a `billing`
    summary (cost vs tokens+time per billing mode, #96) and `elapsed_seconds` so
    the cockpit shows real dollars only for metered work. `by_project` is deferred
    — CFactory has no project dimension on the WorkItem yet.
    """
    services = ("pfactory", "aifactory", "tfactory")
    keys = ("input_tokens", "output_tokens", "total_tokens")
    by_service = {
        s: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "instrumented": False,
        }
        for s in services
    }
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    by_work_item: list[dict] = []

    for wi in store.list():
        wi_tot = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
        budget = None
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
            # Soft budget (v1.3 P2): present only when the contract set one;
            # carried on a service's terminal usage. Surface it on the row so
            # the cockpit can render an "over budget" badge. None on the common
            # (no-budget) case — old rows are unchanged.
            if u.budget is not None:
                budget = u.budget.model_dump()
        if wi_tot["total_tokens"] or wi_tot["cost_usd"]:
            row = {"correlation_key": wi.correlation_key, "title": wi.title, **wi_tot}
            if budget is not None:
                row["budget"] = budget
            billing = _billing_summary(wi)
            if billing is not None:
                row["billing"] = billing
            elapsed = _wi_elapsed_seconds(wi)
            if elapsed is not None:
                row["elapsed_seconds"] = elapsed
            by_work_item.append(row)

    by_work_item.sort(key=lambda w: w["total_tokens"], reverse=True)
    # Headline metered spend across the fleet: real dollars only (api/cloud),
    # so the cockpit can hide the "Cost (USD)" stat when everything ran on a
    # subscription / locally (#96). Falls back to the scalar total when no row
    # carries a billing breakdown (older events).
    metered = round(
        sum(r["billing"]["metered_cost_usd"] for r in by_work_item if "billing" in r), 6
    )
    any_billing = any("billing" in r for r in by_work_item)
    total["metered_cost_usd"] = metered if any_billing else total["cost_usd"]
    total["has_billing_modes"] = any_billing
    return {"total": total, "by_service": by_service, "by_work_item": by_work_item}


def _merge_rollup(dst: dict, src: dict | None) -> None:
    """Sum a per-provider/per-model rollup dict ``src`` into ``dst`` in place."""
    for key, vals in (src or {}).items():
        b = dst.setdefault(key, empty_bucket())
        add_usage(b, vals)


def token_by_worker(store: WorkItemStore) -> dict:
    """Per-worker / per-provider / per-model breakdown (RFC-0001 v1.3).

    Additive companion to ``token_totals``: surfaces the per-worker drill-down
    and the provider/model rollups CFactory ingests from live ``phase:"worker"``
    sub-events + terminal breakdowns. ``by_provider``/``by_model`` are aggregated
    across every work item + service; ``by_work_item`` carries each item's
    per-worker rows for the cockpit drill-down. Empty when nothing is
    instrumented yet — old work items simply contribute no workers.
    """
    services = ("pfactory", "aifactory", "tfactory")
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    items: list[dict] = []

    for wi in store.list():
        workers: list[dict] = []
        for svc in services:
            state = getattr(wi, svc)
            for wid, w in (state.workers or {}).items():
                wd = w if isinstance(w, dict) else w.model_dump()
                workers.append({"service": svc, **wd})
            _merge_rollup(by_provider, state.by_provider)
            _merge_rollup(by_model, state.by_model)
        if workers:
            workers.sort(key=lambda x: x.get("total_tokens", 0), reverse=True)
            items.append(
                {
                    "correlation_key": wi.correlation_key,
                    "title": wi.title,
                    "workers": workers,
                }
            )

    items.sort(
        key=lambda it: sum(w.get("total_tokens", 0) for w in it["workers"]),
        reverse=True,
    )
    return {"by_provider": by_provider, "by_model": by_model, "by_work_item": items}


def worker_progress(store: WorkItemStore, correlation_key: str) -> dict:
    """Rolling per-worker progress series for one running task (Tier 1.5).

    Exposes the ~10s heartbeat series ingested from ``phase:"worker_progress"``
    events so the cockpit can draw a smooth ticking sparkline while a task runs.
    Returns ``{correlation_key, series: [{ts, total_tokens, cost_usd}, ...],
    workers: {worker_id: [points...]}}`` where ``series`` is a single DENSE,
    time-ordered cumulative-across-workers series (the sparkline feed) and
    ``workers`` is the raw per-worker drill-down. Empty (``series: []``) when the
    task is unknown or has no progress yet — the frontend then falls back to the
    stepwise worker-completion series, so this is purely additive."""
    wi = store.get(correlation_key)
    raw: dict[str, list[dict]] = {}
    if wi is not None:
        for svc in ("pfactory", "aifactory", "tfactory"):
            state = getattr(wi, svc)
            for wid, pts in (state.worker_progress or {}).items():
                raw[wid] = [p if isinstance(p, dict) else p.model_dump() for p in pts]

    # Build a single dense series: at each distinct timestamp, sum the
    # latest-known total_tokens/cost_usd across all workers (cumulative across
    # the parallel fleet). This is what the sparkline ticks on. We carry forward
    # each worker's last sample so a quiet worker doesn't drop the running total.
    stamps = sorted({p["ts"] for pts in raw.values() for p in pts})
    series: list[dict] = []
    if stamps:
        # Pre-sort each worker's points by ts so the carry-forward walk is correct.
        ordered = {wid: sorted(pts, key=lambda p: p["ts"]) for wid, pts in raw.items()}
        cursors = dict.fromkeys(ordered, 0)
        last = {wid: {"total_tokens": 0, "cost_usd": 0.0} for wid in ordered}
        for ts in stamps:
            for wid, pts in ordered.items():
                i = cursors[wid]
                while i < len(pts) and pts[i]["ts"] <= ts:
                    last[wid] = {
                        "total_tokens": pts[i].get("total_tokens", 0) or 0,
                        "cost_usd": pts[i].get("cost_usd", 0.0) or 0.0,
                    }
                    i += 1
                cursors[wid] = i
            series.append(
                {
                    "ts": ts,
                    "total_tokens": sum(v["total_tokens"] for v in last.values()),
                    "cost_usd": sum(v["cost_usd"] for v in last.values()),
                }
            )

    return {"correlation_key": correlation_key, "series": series, "workers": raw}


def rollups_summary_line(store: WorkItemStore) -> str:
    """One-line rollups summary for the copilot context."""
    r = rollups(store)
    bs = r["by_stage"]
    return (
        f"Board: {r['total_work_items']} work items "
        f"(plan={bs['plan']}, code={bs['code']}, test={bs['test']}), "
        f"{r['total_events']} events."
    )

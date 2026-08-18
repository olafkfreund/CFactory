"""WorkItem read API: list/detail/timeline/process/evidence plus the derived
rollups, tokens, progress, and anomaly views.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .adapters import BaseHTTPAdapter
from .adapters.tfactory import TFactoryAdapter
from .api_deps import adapters_dep, progress_hub_dep, store_dep
from .copilot.anomalies import detect_anomalies
from .copilot.tools import (
    cost_routing,
    rollups as compute_rollups,
    summarize_timeline,
    token_by_worker,
    token_totals,
    worker_progress,
)
from .needs_you import needs_you_count
from .progress import LiveProgressHub
from .search import search_workitems
from .store import WorkItemStore, compute_liveness
from .task_process import build_process_detail

router = APIRouter(tags=["workitems"])


@router.get("/api/workitems")
def list_workitems(store: Annotated[WorkItemStore, Depends(store_dep)]) -> dict[str, object]:
    items = store.list()
    # Attach a request-time liveness signal per item (#105): last-activity
    # age + a `stalled` flag for a task hung in a non-terminal stage, so the
    # watch plane can alert instead of showing a quiet non-terminal phase.
    out = []
    for wi in items:
        data = wi.model_dump(mode="json")
        data["liveness"] = compute_liveness(wi).model_dump(mode="json")
        out.append(data)
    return {
        "count": len(out),
        "items": out,
        "stalled_count": sum(1 for d in out if d["liveness"]["stalled"]),
    }


@router.get("/api/search")
def search(
    store: Annotated[WorkItemStore, Depends(store_dep)],
    q: str = "",
    limit: int = 20,
) -> dict[str, object]:
    """Federated cross-portal search over the aggregated work-item store (#149).

    Ranks every ingested item (Plan/Build/Test) by relevance to ``q`` and
    returns the top ``limit``; each result deep-links back via its
    ``correlation_key``. A blank ``q`` returns an empty result set.
    """
    results = search_workitems(store.list(), q, max(1, min(limit, 50)))
    return {"query": q, "count": len(results), "results": results}


@router.get("/api/needs-you/count")
def needs_you(store: Annotated[WorkItemStore, Depends(store_dep)]) -> dict[str, int]:
    """Fleet count of work items blocked on a human (review gates + stalls, #148).

    Surfaced as a badge in every portal's shared top bar; the forks proxy this so
    the "N need you" number is consistent across the family.
    """
    return {"count": needs_you_count(store.list())}


@router.get("/api/rollups")
def get_rollups(store: Annotated[WorkItemStore, Depends(store_dep)]) -> dict[str, object]:
    return compute_rollups(store)


@router.get("/api/tokens")
def get_tokens(store: Annotated[WorkItemStore, Depends(store_dep)]) -> dict[str, object]:
    return token_totals(store)


@router.get("/api/tokens/by_worker")
def get_tokens_by_worker(
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> dict[str, object]:
    """Per-worker / per-provider / per-model breakdown (RFC-0001 v1.3).

    Additive to ``/api/tokens`` (which keeps its by_service/by_work_item
    shape): exposes the drill-down ingested from live ``phase:"worker"``
    sub-events + terminal breakdowns. Empty when nothing is instrumented."""
    return token_by_worker(store)


@router.get("/api/tasks/{correlation_key}/worker-progress")
def get_worker_progress(
    correlation_key: str,
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> dict[str, object]:
    """Rolling per-worker progress series for a running task (Tier 1.5).

    Additive to ``/api/tokens/by_worker``: exposes the ~10s heartbeat series
    ingested from ``phase:"worker_progress"`` events as a single dense,
    time-ordered cumulative series (plus the raw per-worker drill-down) so
    the live stamp's sparkline ticks smoothly. ``series: []`` for an unknown
    task or one with no progress yet — the frontend falls back to the
    stepwise worker-completion series, so existing cards are unaffected."""
    return worker_progress(store, correlation_key)


@router.get("/api/tasks/{correlation_key}/cost-routing")
def get_cost_routing(
    correlation_key: str,
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> dict[str, object]:
    """Pre-execution routing estimate vs actual rolled-up spend (RFC-0014 #124).

    Joins PFactory's ``execution.routing`` decision (class, cost estimate vs
    ceiling, per-role models, runtime, budget_mode, autonomy, rationale) with the
    actual RFC-0001 usage CFactory already rolls up (per-model cost + the
    billing-mode summary, real ``$`` only for metered work). ``404`` for an
    unknown work item; the ``routing`` field is ``null`` when no routing block was
    emitted (legacy task) but actual spend is still returned."""
    result = cost_routing(store, correlation_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no work item for {correlation_key!r}")
    return result


@router.get("/api/progress")
def get_progress(
    hub: Annotated[LiveProgressHub, Depends(progress_hub_dep)],
) -> dict[str, object]:
    items = hub.snapshot()
    return {"count": len(items), "items": [p.model_dump(mode="json") for p in items]}


@router.get("/api/anomalies")
def get_anomalies(store: Annotated[WorkItemStore, Depends(store_dep)]) -> dict[str, object]:
    found = detect_anomalies(store)
    return {"count": len(found), "anomalies": found}


@router.get("/api/workitems/{correlation_key}")
def get_workitem(
    correlation_key: str,
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> dict[str, object]:
    wi = store.get(correlation_key)
    if wi is None:
        raise HTTPException(status_code=404, detail=f"no work item for {correlation_key!r}")
    return wi.model_dump(mode="json")


@router.get("/api/workitems/{correlation_key}/timeline")
def get_timeline(
    correlation_key: str,
    store: Annotated[WorkItemStore, Depends(store_dep)],
) -> dict[str, object]:
    summary = summarize_timeline(store, correlation_key)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"no work item for {correlation_key!r}")
    return summary


@router.get("/api/workitems/{correlation_key}/process")
async def get_process(
    correlation_key: str,
    store: Annotated[WorkItemStore, Depends(store_dep)],
    adapters: Annotated[list[BaseHTTPAdapter], Depends(adapters_dep)],
) -> dict[str, object]:
    """Rich live process detail for the work item's code task (#45).

    Proxies + normalizes the active AIFactory task (phase, progress %, current
    subtask, subtask list) from its REST detail endpoint. Best-effort:
    ``available: false`` when there's no task or the service is unreachable."""
    try:
        return await run_in_threadpool(build_process_detail, store, adapters, correlation_key)
    finally:
        for adapter in adapters:
            adapter.close()


@router.get("/api/workitems/{correlation_key}/evidence/{kind}/{name}")
async def get_evidence_media(
    correlation_key: str,
    kind: str,
    name: str,
    store: Annotated[WorkItemStore, Depends(store_dep)],
    adapters: Annotated[list[BaseHTTPAdapter], Depends(adapters_dep)],
) -> Response:
    """Proxy one browser-lane screenshot/recording from TFactory.

    The cockpit is same-origin to CFactory but NOT to TFactory, so an
    ``<img>``/``<video>`` can't carry the TFactory bearer. CFactory fetches the
    bytes with its own upstream token and streams them back, keeping the
    TFactory URL + token inside this process. ``kind`` ∈ {screenshots, videos}.
    """
    if kind not in ("screenshots", "videos"):
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind!r}")
    # Defence in depth — the adapter also scopes to the findings dir, but reject
    # traversal here before any upstream call.
    if "/" in name or ".." in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="invalid artifact name")
    try:
        wi = store.get(correlation_key)
        spec_id = wi.tfactory.task_id if wi else None
        if not spec_id:
            raise HTTPException(status_code=404, detail="no tfactory spec for work item")
        tf = next((a for a in adapters if isinstance(a, TFactoryAdapter)), None)
        if tf is None:
            raise HTTPException(status_code=503, detail="tfactory adapter unavailable")
        media = await run_in_threadpool(tf.fetch_media, spec_id, kind, name)
        if media is None:
            raise HTTPException(status_code=404, detail=f"evidence not found: {kind}/{name}")
        content, content_type = media
        return Response(content=content, media_type=content_type)
    finally:
        for adapter in adapters:
            adapter.close()

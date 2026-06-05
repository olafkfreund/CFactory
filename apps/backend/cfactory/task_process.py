"""Task process detail — rich, live state for one work item's running task (#45).

The cockpit's task-detail drawer wants more than the WorkItem timeline: the
*process* a task is in (phase, % progress, current subtask, the subtask list).
The code stage (AIFactory) already exposes this over plain REST
(``GET /api/tasks/{id}`` → ``executionProgress`` + ``subtasks``), and CFactory
already speaks REST to it via :class:`AIFactoryAdapter` — so we proxy + normalize
that rather than reaching for the siblings' MCP server (cf. DEC-002).

Best-effort: an unknown key, no task id, or an unreachable service yields
``{"available": false}`` (plus whatever slice state we do have) so the drawer
degrades instead of erroring.
"""

from __future__ import annotations

from typing import Any

from .adapters.aifactory import AIFactoryAdapter
from .adapters.base import BaseHTTPAdapter
from .store import WorkItemStore


def _normalize(correlation_key: str, d: dict[str, Any]) -> dict[str, Any]:
    """Map an AIFactory task object to the cockpit's stable process shape."""
    ep = d.get("executionProgress") if isinstance(d.get("executionProgress"), dict) else {}
    raw_subs = d.get("subtasks") if isinstance(d.get("subtasks"), list) else []
    subtasks = [
        {"title": s.get("title"), "status": s.get("status")}
        for s in raw_subs
        if isinstance(s, dict)
    ]
    return {
        "available": True,
        "correlation_key": correlation_key,
        "service": "aifactory",
        "task_id": str(d.get("id") or d.get("specId") or ""),
        "title": d.get("title"),
        "status": d.get("status"),
        "phase": d.get("phase"),
        "progress": {
            "phase": ep.get("phase"),
            "phase_percent": ep.get("phaseProgress"),
            "overall_percent": ep.get("overallProgress"),
            "current_subtask": ep.get("currentSubtask"),
            "message": ep.get("message"),
        },
        "subtasks": subtasks,
        "branch": d.get("branchName"),
        "updated_at": d.get("updatedAt"),
    }


def build_process_detail(
    store: WorkItemStore,
    adapters: list[BaseHTTPAdapter],
    correlation_key: str,
) -> dict[str, Any]:
    """Resolve a work item's code task and return its normalized process detail.

    Focuses on the AIFactory (code) slice — the stage whose process the cockpit
    most wants to watch live. Returns ``available: false`` (with any slice state)
    when there's no work item, no AIFactory task id, or the service is down.
    """
    wi = store.get(correlation_key)
    if wi is None:
        return {"available": False, "correlation_key": correlation_key, "reason": "no_work_item"}

    ai = wi.aifactory
    adapter = next((a for a in adapters if isinstance(a, AIFactoryAdapter)), None)

    detail = None
    if adapter is not None and ai.task_id:
        detail = adapter.get_task_detail(ai.task_id)

    if detail is None:
        # No rich detail (no task id, service down, or old build) — hand back the
        # slice state we already have so the drawer can still show status/phase.
        return {
            "available": False,
            "correlation_key": correlation_key,
            "service": "aifactory",
            "task_id": ai.task_id,
            "status": ai.status,
            "phase": ai.phase,
            "reason": "detail_unavailable",
        }

    return _normalize(correlation_key, detail)

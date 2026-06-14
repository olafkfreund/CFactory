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
from .adapters.pfactory import PFactoryAdapter
from .adapters.tfactory import TFactoryAdapter
from .store import WorkItemStore

# TFactory's v0.2 lane spine, in execution order. Drives the test-stage diagram's
# column ordering + the lane→lane "next" edges. Unknown lanes append after these.
_LANE_SPINE = ["unit", "browser", "api", "integration", "mutation"]


def _build_code_graph(raw_subs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Turn AIFactory subtasks into the cockpit's live-diagram graph (#94).

    Each subtask becomes a node; its ``depends_on`` becomes the edges. Timing
    (``started_at``/``completed_at``) drives the per-node clocks, ``service`` is
    the accent ``kind``. Returns ``None`` when there are no usable nodes so the
    diagram simply doesn't render (additive — older AIFactory builds that don't
    emit subtask ids/deps yield an edgeless-but-still-useful node list).
    """
    nodes: list[dict[str, Any]] = []
    for i, s in enumerate(raw_subs):
        if not isinstance(s, dict):
            continue
        node_id = str(s.get("id") or i)
        label = s.get("title") or s.get("description") or f"subtask {i + 1}"
        deps = s.get("depends_on")
        deps = [str(x) for x in deps] if isinstance(deps, list) else []
        nodes.append(
            {
                "id": node_id,
                "label": str(label)[:80],
                "kind": s.get("service"),
                "status": s.get("status"),
                "started_at": s.get("started_at"),
                "completed_at": s.get("completed_at"),
                "deps": deps,
            }
        )
    if not nodes:
        return None
    return {"stage": "code", "nodes": nodes}


_FAIL_WORDS = ("fail", "reject", "error", "abort", "cancel", "block", "discard")
_ACTIVE_WORDS = (
    "progress", "running", "review", "triag", "generat", "coding",
    "planning", "executing", "await", "building", "queued", "backlog",
)
_DONE_WORDS = (
    "done", "complete", "passed", "merged", "approved", "emitted",
    "success", "closed", "shipped",
)


def _plan_child_state(wi: Any) -> tuple[str | None, str | None, str | None]:
    """Derive ``(status, started_at, completed_at)`` for one plan child from its
    downstream WorkItem — the GitHub issue PFactory emitted for that child, which
    AIFactory/TFactory then execute. The plan node lights up from the furthest the
    child has actually reached: failed if any stage failed, in_progress if any is
    engaged, completed once a stage reports terminal success (#94). Timing spans
    the child's first → last completion event so the plan node gets a live clock."""
    states = [getattr(wi, "pfactory", None), getattr(wi, "aifactory", None), getattr(wi, "tfactory", None)]
    statuses = [s.status.lower() for s in states if s and getattr(s, "status", None)]

    status: str | None = None
    if any(w in s for s in statuses for w in _FAIL_WORDS):
        status = "failed"
    elif any(w in s for s in statuses for w in _DONE_WORDS):
        status = "completed"
    elif any(w in s for s in statuses for w in _ACTIVE_WORDS):
        status = "in_progress"
    elif statuses:
        status = "in_progress"  # engaged but unrecognised — treat as live

    started_at = completed_at = None
    times = sorted(
        e.updated_at for e in (getattr(wi, "timeline", None) or []) if getattr(e, "updated_at", None)
    )
    if times:
        started_at = times[0].isoformat()
        if status in ("completed", "failed"):
            completed_at = times[-1].isoformat()
    return status, started_at, completed_at


def _build_plan_graph(session: dict[str, Any], store: WorkItemStore | None = None) -> dict[str, Any] | None:
    """Turn a PFactory plan session's decomposed ``epic.children`` into the
    plan-stage diagram graph (#94): one node per child, ``depends_on`` → edges,
    ``kind`` (feature/testing/cicd/infra/docs) as the accent.

    Each child is emitted as its own GitHub issue (``emit_result.child_numbers``
    maps child key → issue#); when ``store`` is supplied we look that issue up and
    light the node from the child's live downstream state, so the plan view shows
    children turning green/active/failed as the build progresses. Children with no
    emitted issue yet (or before emit) stay null = planned/waiting. ``None`` when
    there's no epic to draw."""
    epic = session.get("epic") if isinstance(session.get("epic"), dict) else None
    children = epic.get("children") if epic and isinstance(epic.get("children"), list) else []
    emit = session.get("emit_result") if isinstance(session.get("emit_result"), dict) else {}
    child_numbers = emit.get("child_numbers") if isinstance(emit.get("child_numbers"), dict) else {}

    nodes: list[dict[str, Any]] = []
    for i, c in enumerate(children):
        if not isinstance(c, dict):
            continue
        key = str(c.get("key") or i)
        deps = c.get("depends_on")
        deps = [str(x) for x in deps] if isinstance(deps, list) else []

        # Light the node from the child's downstream WorkItem when we can map it
        # to its emitted issue#; otherwise it renders as a planned (waiting) unit.
        status = started_at = completed_at = None
        issue = child_numbers.get(key)
        if issue is not None and store is not None:
            wi = store.get(str(issue))
            if wi is not None:
                status, started_at, completed_at = _plan_child_state(wi)

        nodes.append(
            {
                "id": key,
                "label": str(c.get("title") or key)[:80],
                "kind": c.get("kind"),
                "status": status,
                "started_at": started_at,
                "completed_at": completed_at,
                "deps": deps,
            }
        )
    if not nodes:
        return None
    return {"stage": "plan", "nodes": nodes}


def _normalize_plan(
    correlation_key: str, session: dict[str, Any], store: WorkItemStore | None = None
) -> dict[str, Any] | None:
    """Plan-stage process detail from a PFactory session — used as a fallback so
    a work item still in (or just past) planning shows its plan DAG, with children
    lit from their live downstream state when ``store`` is supplied. ``None`` when
    the session has no decomposed epic to draw."""
    graph = _build_plan_graph(session, store)
    if graph is None:
        return None
    return {
        "available": True,
        "correlation_key": correlation_key,
        "service": "pfactory",
        "task_id": str(session.get("session_id") or session.get("id") or ""),
        "title": session.get("title"),
        "status": session.get("board_state") or session.get("status"),
        "phase": "plan",
        "graph": graph,
        "updated_at": session.get("updated_at") or session.get("updatedAt"),
    }


def _lane_status(statuses: list[str]) -> str | None:
    """Aggregate a lane's subtask statuses into one node status, worst-first so a
    single failure/stall/active subtask colours the whole lane (#94). A lane is
    only ``done`` when every subtask completed; empty/unknown → pending (None)."""
    s = {str(x or "").lower() for x in statuses}
    if any("fail" in x or "error" in x for x in s):
        return "failed"
    if any("stuck" in x or "stall" in x or "block" in x for x in s):
        return "stalled"
    if any("progress" in x or "running" in x for x in s):
        return "active"
    if statuses and all("complet" in str(x or "").lower() or "passed" in str(x or "").lower() for x in statuses):
        return "completed"
    return None


def _build_test_graph(raw_subs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Aggregate TFactory's lane-tagged subtasks into the test-stage diagram: one
    node per lane (unit→browser→api→integration→mutation), spine-ordered with
    lane→lane edges, each lane's status rolled up from its subtasks and its timing
    spanning earliest start → latest finish (#94). ``None`` with no lanes."""
    lanes: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for s in raw_subs:
        if not isinstance(s, dict):
            continue
        lane = str(s.get("lane") or "unit").lower()
        if lane not in lanes:
            lanes[lane] = []
            order.append(lane)
        lanes[lane].append(s)

    if not lanes:
        return None

    # Spine lanes first (in canonical order), then any unknown lanes as seen.
    ordered = [ln for ln in _LANE_SPINE if ln in lanes] + [ln for ln in order if ln not in _LANE_SPINE]

    nodes: list[dict[str, Any]] = []
    prev: str | None = None
    for lane in ordered:
        members = lanes[lane]
        status = _lane_status([m.get("status") for m in members])
        starts = [m.get("started_at") for m in members if m.get("started_at")]
        completes = [m.get("completed_at") for m in members if m.get("completed_at")]
        done_count = sum(1 for m in members if "complet" in str(m.get("status") or "").lower())
        nodes.append(
            {
                "id": lane,
                "label": f"{lane.capitalize()} ({done_count}/{len(members)})",
                "kind": lane,
                "status": status,
                "started_at": min(starts) if starts else None,
                # Only call a lane finished (latest completion) when all its
                # subtasks are done — else the timer should keep running.
                "completed_at": max(completes) if completes and status == "completed" else None,
                "deps": [prev] if prev else [],
            }
        )
        prev = lane

    return {"stage": "test", "nodes": nodes}


def _normalize_test(correlation_key: str, d: dict[str, Any]) -> dict[str, Any] | None:
    """Test-stage process detail from a TFactory task — the lane pipeline diagram.
    ``None`` when there are no lane subtasks to aggregate."""
    raw_subs = d.get("subtasks") if isinstance(d.get("subtasks"), list) else []
    graph = _build_test_graph(raw_subs)
    if graph is None:
        return None
    return {
        "available": True,
        "correlation_key": correlation_key,
        "service": "tfactory",
        "task_id": str(d.get("id") or d.get("spec_id") or d.get("specId") or ""),
        "title": d.get("title"),
        "status": d.get("status"),
        "phase": d.get("phase") or "test",
        "graph": graph,
        "branch": d.get("branchName"),
        "updated_at": d.get("updatedAt") or d.get("updated_at"),
    }


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
        "graph": _build_code_graph(raw_subs),
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

    # Stage preference: test → code → plan. The furthest-along stage with real
    # detail wins, so a testing item shows its live lane pipeline, a coding item
    # the code DAG, and a planning item the plan DAG (#94).
    tf = wi.tfactory
    tf_adapter = next((a for a in adapters if isinstance(a, TFactoryAdapter)), None)
    if tf_adapter is not None and tf.task_id:
        tdetail = tf_adapter.get_test_detail(tf.task_id)
        if tdetail is not None:
            test = _normalize_test(correlation_key, tdetail)
            if test is not None:
                return test

    ai = wi.aifactory
    adapter = next((a for a in adapters if isinstance(a, AIFactoryAdapter)), None)

    detail = None
    if adapter is not None and ai.task_id:
        detail = adapter.get_task_detail(ai.task_id)

    if detail is not None:
        return _normalize(correlation_key, detail)

    # No code detail yet (item still planning, no AIFactory task id, service down,
    # or old build). Fall back to the plan stage: draw the PFactory plan DAG so a
    # plan-stage item still gets a live diagram (#94). Best-effort.
    pf = wi.pfactory
    pf_adapter = next((a for a in adapters if isinstance(a, PFactoryAdapter)), None)
    if pf_adapter is not None and pf.task_id:
        session = pf_adapter.get_session_detail(pf.task_id)
        if session is not None:
            plan = _normalize_plan(correlation_key, session, store)
            if plan is not None:
                return plan

    # Nothing rich to show — hand back the slice state we already have so the
    # drawer can still show status/phase.
    return {
        "available": False,
        "correlation_key": correlation_key,
        "service": "aifactory",
        "task_id": ai.task_id,
        "status": ai.status,
        "phase": ai.phase,
        "reason": "detail_unavailable",
    }

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
    "progress",
    "running",
    "review",
    "triag",
    "generat",
    "coding",
    "planning",
    "executing",
    "await",
    "building",
    "queued",
    "backlog",
)
_DONE_WORDS = (
    "done",
    "complete",
    "passed",
    "merged",
    "approved",
    "emitted",
    "success",
    "closed",
    "shipped",
)


def _plan_child_state(wi: Any) -> tuple[str | None, str | None, str | None]:
    """Derive ``(status, started_at, completed_at)`` for one plan child from its
    downstream WorkItem — the GitHub issue PFactory emitted for that child, which
    AIFactory/TFactory then execute. The plan node lights up from the furthest the
    child has actually reached: failed if any stage failed, in_progress if any is
    engaged, completed once a stage reports terminal success (#94). Timing spans
    the child's first → last completion event so the plan node gets a live clock."""
    states = [
        getattr(wi, "pfactory", None),
        getattr(wi, "aifactory", None),
        getattr(wi, "tfactory", None),
    ]
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
        e.updated_at
        for e in (getattr(wi, "timeline", None) or [])
        if getattr(e, "updated_at", None)
    )
    if times:
        started_at = times[0].isoformat()
        if status in ("completed", "failed"):
            completed_at = times[-1].isoformat()
    return status, started_at, completed_at


def _plan_stage_done(session: dict[str, Any]) -> bool:
    """True when the plan session itself reached a clean terminal state — its
    decomposition was reviewed, approved and emitted without failing. Lets the
    plan-flow nodes light green as *planned cleanly* even before any downstream
    build state exists, so a finished plan shows every unit that passed planning.
    A failed or still-in-progress plan (e.g. ``human_review``) returns False, so
    those nodes stay pending. This is a plan-stage claim (successfully planned),
    never a verification claim — downstream state still overrides it per node."""
    raw = str(session.get("board_state") or session.get("status") or "").lower()
    if not raw or any(w in raw for w in _FAIL_WORDS):
        return False
    return any(w in raw for w in _DONE_WORDS)


def _build_plan_graph(
    session: dict[str, Any], store: WorkItemStore | None = None
) -> dict[str, Any] | None:
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
    # Whole-plan clean-completion signal: when set, every unit that has no richer
    # downstream state is shown as done (it passed planning) so the plan DAG goes
    # green, matching the "stage complete" frame.
    plan_done = _plan_stage_done(session)

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

        # No richer downstream state, but the plan finished cleanly → this unit
        # was successfully planned & emitted: mark it done so the node goes green.
        # Downstream failure/activity (resolved above) always takes precedence.
        if status is None and plan_done:
            status = "completed"

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
    if statuses and all(
        "complet" in str(x or "").lower() or "passed" in str(x or "").lower() for x in statuses
    ):
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
    ordered = [ln for ln in _LANE_SPINE if ln in lanes] + [
        ln for ln in order if ln not in _LANE_SPINE
    ]

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


def _extract_artifacts(*sources: dict[str, Any] | None) -> dict[str, str] | None:
    """RFC-0015 §3.3: pull the human-readable spec/plan/tasks Markdown mirror a
    producer (PFactory emit) may carry on a session/task object, so the cockpit
    can render the plan as docs instead of stringified structures.

    Tolerant of two shapes — top-level ``spec_md``/``plan_md``/``tasks_md`` or a
    nested ``artifacts: {spec, plan, tasks}`` map — and picks the first non-empty
    value across all sources. Returns ``None`` when nothing usable is present, so
    the artifacts panel simply doesn't render (additive — a producer that doesn't
    emit the mirror looks exactly as before).
    """
    out: dict[str, str] = {}
    aliases = {
        "spec": ("spec_md", "specMd", "spec_markdown"),
        "plan": ("plan_md", "planMd", "plan_markdown"),
        "tasks": ("tasks_md", "tasksMd", "tasks_markdown"),
    }
    for src in sources:
        if not isinstance(src, dict):
            continue
        nested = src.get("artifacts") if isinstance(src.get("artifacts"), dict) else {}
        for key, keys in aliases.items():
            if key in out:
                continue
            val: Any = None
            for k in keys:
                if isinstance(src.get(k), str) and src[k].strip():
                    val = src[k]
                    break
            if val is None and isinstance(nested.get(key), str) and nested[key].strip():
                val = nested[key]
            if isinstance(val, str) and val.strip():
                out[key] = val
    return out or None


def _extract_traceability(*sources: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """RFC-0015 §4 D2: pull the requirement->test traceability rows a verifier
    (TFactory) may attach — either at the top level (``traceability``) or under a
    gate-normalized ``verification`` block. Each row is {ac_id, ac_text?, tests[],
    val_level?, status}. Returns ``None`` when absent so the matrix degrades to a
    "not available" note rather than rendering an empty table.
    """
    for src in sources:
        if not isinstance(src, dict):
            continue
        candidates = [src.get("traceability")]
        ver = src.get("verification")
        if isinstance(ver, dict):
            candidates.append(ver.get("traceability"))
        for c in candidates:
            if isinstance(c, list) and c:
                rows = [r for r in c if isinstance(r, dict) and r.get("ac_id")]
                if rows:
                    return rows
    return None


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


def _rfc0015_extras(
    tf: Any,
    session_raw: dict[str, Any] | None,
    tdetail_raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Mine the RFC-0015 cockpit extras — readable spec/plan/tasks Markdown (§3.3)
    and the requirement->test traceability rows (§4 D2) — from the raw upstream
    payloads, falling back to the tfactory slice's stored verification block for
    traceability. Returns only the keys actually present, so the caller can splat
    it onto a detail dict without adding empty fields (all additive)."""
    tf_extra = getattr(tf, "extra", None)
    tf_verification = tf_extra.get("verification") if isinstance(tf_extra, dict) else None
    artifacts = _extract_artifacts(session_raw, tdetail_raw)
    traceability = _extract_traceability(
        tdetail_raw, {"verification": tf_verification} if tf_verification else None
    )
    extras: dict[str, Any] = {}
    if artifacts:
        extras["artifacts"] = artifacts
    if traceability:
        extras["traceability"] = traceability
    return extras


def _fetch_test_stage(
    tf_adapter: TFactoryAdapter | None,
    task_id: str | None,
    correlation_key: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch the TFactory (test) slice: ``(evidence, raw detail, normalized stage)``.

    Browser-lane evidence (screenshots + recordings) is fetched independently of
    the lane graph so the cockpit can show the captured proof even when the graph
    can't be aggregated. CFactory proxies the bytes (the browser is authenticated
    to CFactory, not TFactory): see the
    /api/workitems/{key}/evidence/{kind}/{name} route.
    """
    if tf_adapter is None or not task_id:
        return None, None, None
    evidence: dict[str, Any] | None = None
    manifest = tf_adapter.get_evidence_manifest(task_id)
    if manifest.get("screenshots") or manifest.get("videos"):
        evidence = {
            "spec_id": task_id,
            "screenshots": manifest["screenshots"],
            "videos": manifest["videos"],
        }
    tdetail = tf_adapter.get_test_detail(task_id)
    test = _normalize_test(correlation_key, tdetail) if tdetail is not None else None
    return evidence, tdetail, test


def _fetch_code_stage(
    ai_adapter: AIFactoryAdapter | None,
    task_id: str | None,
    correlation_key: str,
) -> dict[str, Any] | None:
    """Fetch + normalize the AIFactory (code) slice, best-effort."""
    if ai_adapter is None or not task_id:
        return None
    code_detail = ai_adapter.get_task_detail(task_id)
    return _normalize(correlation_key, code_detail) if code_detail is not None else None


def _fetch_plan_stage(
    pf_adapter: PFactoryAdapter | None,
    task_id: str | None,
    correlation_key: str,
    store: WorkItemStore,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch the PFactory (plan) slice: ``(raw session, normalized stage)``."""
    if pf_adapter is None or not task_id:
        return None, None
    session = pf_adapter.get_session_detail(task_id)
    if session is None:
        return None, None
    return session, _normalize_plan(correlation_key, session, store)


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

    # Build EVERY available stage's process detail (plan/code/test), not just the
    # furthest, so the cockpit can offer a stage switcher and the plan DAG stays
    # visible after coding begins (#94 follow-up). Each fetch is best-effort.
    # The raw upstream payloads are kept so we can mine RFC-0015 extras (readable
    # spec/plan/tasks artifacts §3.3, requirement->test traceability §4 D2)
    # without re-fetching. All best-effort + additive.
    tf = wi.tfactory
    ai = wi.aifactory
    tf_adapter = next((a for a in adapters if isinstance(a, TFactoryAdapter)), None)
    ai_adapter = next((a for a in adapters if isinstance(a, AIFactoryAdapter)), None)
    pf_adapter = next((a for a in adapters if isinstance(a, PFactoryAdapter)), None)

    tf_evidence, tdetail_raw, test = _fetch_test_stage(tf_adapter, tf.task_id, correlation_key)
    code = _fetch_code_stage(ai_adapter, ai.task_id, correlation_key)
    session_raw, plan = _fetch_plan_stage(pf_adapter, wi.pfactory.task_id, correlation_key, store)

    stages: dict[str, dict[str, Any]] = {}  # stage -> normalized process detail
    if test is not None:
        stages["test"] = test
    if code is not None:
        stages["code"] = code
    if plan is not None:
        stages["plan"] = plan

    # RFC-0015 cockpit extras (readable artifacts §3.3 + traceability §4 D2),
    # mined from the raw upstream payloads. Best-effort + additive.
    extras = _rfc0015_extras(tf, session_raw, tdetail_raw)

    if stages:
        # Primary = the furthest stage present (test > code > plan): its top-level
        # fields + `graph` are the default view (back-compat). `graphs` carries
        # each present stage's graph so the modal can switch between them.
        order = ("test", "code", "plan")
        primary = dict(stages[next(s for s in order if s in stages)])
        graphs = {s: d["graph"] for s, d in stages.items() if d.get("graph")}
        if graphs:
            primary["graphs"] = graphs
        if tf_evidence:
            primary["evidence"] = tf_evidence
        primary.update(extras)
        return primary

    # Nothing rich to show — hand back the slice state we already have so the
    # drawer can still show status/phase (+ any captured evidence).
    fallback = {
        "available": bool(tf_evidence or extras),
        "correlation_key": correlation_key,
        "service": "aifactory",
        "task_id": ai.task_id,
        "status": ai.status,
        "phase": ai.phase,
        "reason": "detail_unavailable",
    }
    fallback.update(extras)
    if tf_evidence:
        fallback["evidence"] = tf_evidence
    return fallback

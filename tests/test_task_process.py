"""Tests for task process detail: GET /api/workitems/{key}/process (#45)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from cfactory.adapters import AIFactoryAdapter
from cfactory.adapters.pfactory import PFactoryAdapter
from cfactory.adapters.tfactory import TFactoryAdapter
from cfactory.app import adapters_dep, create_app, store_dep
from cfactory.models import CompletionEvent, Service
from cfactory.task_process import (
    _extract_artifacts,
    _extract_traceability,
    build_process_detail,
)

_DETAIL = {
    "id": "proj:spec-1",
    "specId": "spec-1",
    "title": "Add /status endpoint",
    "status": "in_progress",
    "phase": "coding",
    "branchName": "feat/spec-1",
    "updatedAt": "2026-06-05T12:00:00Z",
    "executionProgress": {
        "phase": "coding",
        "phaseProgress": 40,
        "overallProgress": 55,
        "currentSubtask": "Wire the route",
        "message": "2/5 subtasks completed",
    },
    "subtasks": [
        {
            "id": "s1",
            "title": "Model",
            "status": "completed",
            "service": "backend",
            "started_at": "2026-06-05T11:50:00Z",
            "completed_at": "2026-06-05T11:55:00Z",
        },
        {
            "id": "s2",
            "title": "Wire the route",
            "status": "in_progress",
            "depends_on": ["s1"],
            "started_at": "2026-06-05T11:56:00Z",
        },
    ],
}


def _detail_transport(payload=_DETAIL, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/tasks/"):
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _seed(store):
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="7",
            service=Service.AIFACTORY,
            task_id="proj:spec-1",
            status="coding",
            phase="coding",
            updated_at=datetime.now(timezone.utc),
        )
    )


# --- unit ------------------------------------------------------------------


def test_build_process_normalizes_detail(store):
    _seed(store)
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport())
    out = build_process_detail(store, [ai], "7")
    assert out["available"] is True
    assert out["progress"]["overall_percent"] == 55
    assert out["progress"]["current_subtask"] == "Wire the route"
    assert [s["status"] for s in out["subtasks"]] == ["completed", "in_progress"]


def test_build_process_emits_code_graph(store):
    """The normalized detail carries a live-diagram graph built from subtasks:
    one node per subtask, depends_on → edges, timing + service preserved (#94)."""
    _seed(store)
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport())
    out = build_process_detail(store, [ai], "7")
    graph = out["graph"]
    assert graph is not None
    assert graph["stage"] == "code"
    ids = [n["id"] for n in graph["nodes"]]
    assert ids == ["s1", "s2"]
    s1, s2 = graph["nodes"]
    assert s1["status"] == "completed" and s1["kind"] == "backend"
    assert s1["completed_at"] == "2026-06-05T11:55:00Z"
    assert s2["deps"] == ["s1"]  # edge s1 → s2
    assert s2["started_at"] == "2026-06-05T11:56:00Z"


def test_build_process_graph_none_without_subtasks(store):
    """No subtasks → graph is None (additive: the diagram won't render)."""
    _seed(store)
    payload = {**_DETAIL, "subtasks": []}
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport(payload=payload))
    out = build_process_detail(store, [ai], "7")
    assert out["graph"] is None


_PLAN_SESSION = {
    "session_id": "sess-1",
    "title": "Add /status endpoint",
    "board_state": "human_review",
    "epic": {
        "plan_id": "p1",
        "epic_title": "Add /status endpoint",
        "children": [
            {"key": "C1", "title": "Model", "kind": "feature", "depends_on": []},
            {"key": "C2", "title": "Route", "kind": "feature", "depends_on": ["C1"]},
            {"key": "C3", "title": "Tests", "kind": "testing", "depends_on": ["C2"]},
        ],
    },
    # Each child was emitted as its own issue (key -> issue#); CFactory lights the
    # plan node from that issue's downstream WorkItem (#94).
    "emit_result": {"child_numbers": {"C1": 101, "C2": 102, "C3": 103}},
}


def _plan_transport(payload=_PLAN_SESSION, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/plan/sessions/"):
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _seed_plan(store):
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="9",
            service=Service.PFACTORY,
            task_id="sess-1",
            status="human_review",
            phase="plan",
            updated_at=datetime.now(timezone.utc),
        )
    )


def test_build_process_falls_back_to_plan_graph(store):
    """A plan-stage item (no AIFactory task) draws the PFactory plan DAG: one
    node per epic child, depends_on → edges, kind preserved (#94)."""
    _seed_plan(store)
    pf = PFactoryAdapter("http://pf", transport=_plan_transport())
    out = build_process_detail(store, [pf], "9")
    assert out["available"] is True
    assert out["service"] == "pfactory"
    graph = out["graph"]
    assert graph["stage"] == "plan"
    assert [n["id"] for n in graph["nodes"]] == ["C1", "C2", "C3"]
    assert graph["nodes"][2]["kind"] == "testing"
    assert graph["nodes"][1]["deps"] == ["C1"]  # C1 → C2 edge
    # No downstream WorkItems seeded for the child issues → nodes stay planned.
    assert all(n["status"] is None for n in graph["nodes"])


def test_plan_children_light_from_downstream_workitems(store):
    """Plan nodes light up from each child's emitted issue#: C1's WorkItem is done
    (green), C2's is mid-build (active), C3 has none yet (planned) (#94)."""
    _seed_plan(store)
    # C1 (issue 101) finished its code stage; C2 (issue 102) is coding; C3 (103) none.
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="101",
            service=Service.AIFACTORY,
            task_id="t101",
            status="done",
            phase="coding",
            updated_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        )
    )
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="102",
            service=Service.AIFACTORY,
            task_id="t102",
            status="in_progress",
            phase="coding",
            updated_at=datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc),
        )
    )
    pf = PFactoryAdapter("http://pf", transport=_plan_transport())
    out = build_process_detail(store, [pf], "9")
    by_id = {n["id"]: n for n in out["graph"]["nodes"]}
    assert by_id["C1"]["status"] == "completed"  # downstream done -> green
    assert by_id["C1"]["started_at"] == "2026-06-05T12:00:00+00:00"
    assert by_id["C1"]["completed_at"] == "2026-06-05T12:00:00+00:00"  # terminal -> stamped
    assert by_id["C2"]["status"] == "in_progress"  # downstream coding -> active
    assert by_id["C2"]["completed_at"] is None  # still running -> no end stamp
    assert by_id["C3"]["status"] is None  # no emitted WorkItem -> planned/waiting


def test_plan_nodes_go_green_when_plan_done(store):
    """A cleanly finished plan lights every unit green (planned & emitted), even
    with no downstream build yet — so the plan DAG matches its 'stage complete'
    frame instead of showing all-grey under a done plan."""
    _seed_plan(store)
    done_session = {**_PLAN_SESSION, "board_state": "emitted"}
    pf = PFactoryAdapter("http://pf", transport=_plan_transport(payload=done_session))
    out = build_process_detail(store, [pf], "9")
    nodes = out["graph"]["nodes"]
    assert nodes and all(n["status"] == "completed" for n in nodes)  # every unit green


def test_plan_done_does_not_mask_downstream_failure(store):
    """The clean-plan default never overrides a real per-node signal: when the plan
    is done but a child's build failed downstream, that node shows failed while the
    untouched units stay green."""
    _seed_plan(store)
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="102",  # C2's emitted issue
            service=Service.AIFACTORY,
            task_id="t102",
            status="failed",
            phase="coding",
            updated_at=datetime.fromisoformat("2026-06-05T12:30:00+00:00"),
        )
    )
    done_session = {**_PLAN_SESSION, "board_state": "emitted"}
    pf = PFactoryAdapter("http://pf", transport=_plan_transport(payload=done_session))
    out = build_process_detail(store, [pf], "9")
    by_id = {n["id"]: n for n in out["graph"]["nodes"]}
    assert by_id["C2"]["status"] == "failed"  # downstream failure wins over clean-plan
    assert by_id["C1"]["status"] == "completed"  # clean unit still green
    assert by_id["C3"]["status"] == "completed"


def test_plan_nodes_stay_pending_while_plan_in_review(store):
    """An in-progress plan (human_review) is NOT done, so its units stay pending —
    the green default only fires on a terminal-clean plan."""
    _seed_plan(store)
    pf = PFactoryAdapter("http://pf", transport=_plan_transport())  # board_state=human_review
    out = build_process_detail(store, [pf], "9")
    assert all(n["status"] is None for n in out["graph"]["nodes"])


_TEST_DETAIL = {
    "id": "tspec-1",
    "spec_id": "tspec-1",
    "title": "Verify /status endpoint",
    "status": "in_progress",
    "phase": "browser",
    "subtasks": [
        {
            "id": "u1",
            "lane": "unit",
            "status": "completed",
            "started_at": "2026-06-05T12:00:00Z",
            "completed_at": "2026-06-05T12:02:00Z",
        },
        {
            "id": "u2",
            "lane": "unit",
            "status": "completed",
            "started_at": "2026-06-05T12:01:00Z",
            "completed_at": "2026-06-05T12:03:00Z",
        },
        {
            "id": "b1",
            "lane": "browser",
            "status": "in_progress",
            "started_at": "2026-06-05T12:03:30Z",
        },
        {"id": "m1", "lane": "mutation", "status": "stuck"},
    ],
}


def _test_transport(payload=_TEST_DETAIL, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/tasks/"):
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _seed_test(store):
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="11",
            service=Service.TFACTORY,
            task_id="tspec-1",
            status="in_progress",
            phase="browser",
            updated_at=datetime.now(timezone.utc),
        )
    )


def test_build_process_emits_test_lane_graph(store):
    """A test-stage item draws the lane pipeline: one node per lane in spine
    order, lane→lane edges, rolled-up status + timing, stuck → stalled (#94)."""
    _seed_test(store)
    tf = TFactoryAdapter("http://tf", transport=_test_transport())
    out = build_process_detail(store, [tf], "11")
    assert out["available"] is True and out["service"] == "tfactory"
    graph = out["graph"]
    assert graph["stage"] == "test"
    by_id = {n["id"]: n for n in graph["nodes"]}
    # Spine order: unit before browser before mutation.
    assert [n["id"] for n in graph["nodes"]] == ["unit", "browser", "mutation"]
    assert by_id["unit"]["status"] == "completed"
    assert by_id["unit"]["label"] == "Unit (2/2)"
    assert by_id["unit"]["started_at"] == "2026-06-05T12:00:00Z"  # earliest start
    assert by_id["unit"]["completed_at"] == "2026-06-05T12:03:00Z"  # latest finish
    assert by_id["browser"]["status"] == "active"
    assert by_id["browser"]["completed_at"] is None  # still running → no end stamp
    assert by_id["browser"]["deps"] == ["unit"]  # lane spine edge
    assert by_id["mutation"]["status"] == "stalled"  # stuck → stalled


def _evidence_transport():
    """Mock that also answers the evidence manifest GET with screenshots + videos."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tfactory/tasks/tspec-1":
            return httpx.Response(
                200,
                json={
                    "artefacts": {
                        "screenshots": {"exists": True, "files": ["root.png", "ping.png"]},
                        "videos": {"exists": True, "files": ["ping.webm"]},
                    }
                },
            )
        if path.startswith("/api/tasks/"):
            return httpx.Response(200, json=_TEST_DETAIL)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def test_build_process_surfaces_browser_evidence(store):
    """The test-stage detail carries the browser-lane screenshots + recordings
    TFactory captured, so the cockpit can render them on the finished task."""
    _seed_test(store)
    tf = TFactoryAdapter("http://tf", transport=_evidence_transport())
    out = build_process_detail(store, [tf], "11")
    ev = out["evidence"]
    assert ev["spec_id"] == "tspec-1"
    assert ev["screenshots"] == ["root.png", "ping.png"]
    assert ev["videos"] == ["ping.webm"]


def test_build_process_no_evidence_key_when_none_captured(store):
    """No evidence block when TFactory reports no media (manifest 404/empty)."""
    _seed_test(store)
    tf = TFactoryAdapter("http://tf", transport=_test_transport())
    out = build_process_detail(store, [tf], "11")
    assert "evidence" not in out


def test_test_stage_wins_over_code(store):
    """When both a TFactory and AIFactory task exist, the furthest stage (test)
    is shown — its lane graph, not the code DAG."""
    _seed_test(store)
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="11",
            service=Service.AIFACTORY,
            task_id="proj:spec-1",
            status="done",
            phase="coding",
            updated_at=datetime.now(timezone.utc),
        )
    )
    tf = TFactoryAdapter("http://tf", transport=_test_transport())
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport())
    out = build_process_detail(store, [tf, ai], "11")
    assert out["graph"]["stage"] == "test"


def test_build_process_emits_all_stage_graphs(store):
    """A work item with both a plan session and a code task exposes BOTH graphs in
    `graphs` (so the modal can switch stages), with `graph` = the furthest (code)."""
    # Plan session + code task under the same correlation_key.
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="20",
            service=Service.PFACTORY,
            task_id="sess-1",
            status="human_review",
            phase="plan",
            updated_at=datetime.now(timezone.utc),
        )
    )
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="20",
            service=Service.AIFACTORY,
            task_id="proj:spec-1",
            status="in_progress",
            phase="coding",
            updated_at=datetime.now(timezone.utc),
        )
    )
    pf = PFactoryAdapter("http://pf", transport=_plan_transport())
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport())
    out = build_process_detail(store, [pf, ai], "20")
    assert set(out["graphs"].keys()) == {"code", "plan"}
    assert out["graphs"]["plan"]["stage"] == "plan"
    assert out["graphs"]["code"]["stage"] == "code"
    assert out["graph"]["stage"] == "code"  # furthest stage is the default view


def test_build_process_no_work_item(store):
    out = build_process_detail(store, [], "nope")
    assert out["available"] is False
    assert out["reason"] == "no_work_item"


def test_build_process_service_down_falls_back_to_slice(store):
    _seed(store)
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport(status=500))
    out = build_process_detail(store, [ai], "7")
    assert out["available"] is False
    assert out["reason"] == "detail_unavailable"
    assert out["status"] == "coding"  # slice state still surfaced


# --- route -----------------------------------------------------------------


def test_route_returns_process(store):
    _seed(store)
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport())
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[adapters_dep] = lambda: [ai]
    body = TestClient(app).get("/api/workitems/7/process").json()
    assert body["available"] is True
    assert body["progress"]["phase_percent"] == 40


# --- RFC-0015: readable artifacts (§3.3) + traceability matrix (§4 D2) -------


def test_extract_artifacts_top_level_and_nested():
    # Top-level *_md fields.
    out = _extract_artifacts({"spec_md": "# Spec", "plan_md": "# Plan"})
    assert out == {"spec": "# Spec", "plan": "# Plan"}

    # Nested artifacts map, and first-non-empty across sources.
    out = _extract_artifacts(
        {"artifacts": {"tasks": "- [ ] a"}},
        {"spec_md": "# Spec", "tasks_md": "ignored — already filled"},
    )
    assert out == {"tasks": "- [ ] a", "spec": "# Spec"}


def test_extract_artifacts_absent_or_blank_returns_none():
    assert _extract_artifacts(None) is None
    assert _extract_artifacts({}) is None
    assert _extract_artifacts({"spec_md": "   "}) is None  # blank treated as absent


def test_extract_traceability_top_level_and_verification_block():
    rows = [{"ac_id": "AC-1", "tests": ["t::ok"], "val_level": "VAL-2", "status": "passed"}]
    assert _extract_traceability({"traceability": rows}) == rows
    # Under a gate-normalized verification block.
    assert _extract_traceability({"verification": {"traceability": rows}}) == rows


def test_extract_traceability_drops_rows_without_ac_id_and_handles_absent():
    assert _extract_traceability(None) is None
    assert _extract_traceability({"traceability": []}) is None
    assert _extract_traceability({"traceability": [{"status": "passed"}]}) is None  # no ac_id

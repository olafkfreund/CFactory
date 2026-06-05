"""Tests for task process detail: GET /api/workitems/{key}/process (#45)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from cfactory.adapters import AIFactoryAdapter
from cfactory.app import adapters_dep, create_app, store_dep
from cfactory.models import CompletionEvent, Service
from cfactory.task_process import build_process_detail

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
        {"title": "Model", "status": "completed"},
        {"title": "Wire the route", "status": "in_progress"},
    ],
}


def _detail_transport(payload=_DETAIL, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/tasks/"):
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _seed(store):
    store.upsert_from_event(CompletionEvent(
        correlation_key="7", service=Service.AIFACTORY, task_id="proj:spec-1",
        status="coding", phase="coding", updated_at=datetime.now(timezone.utc)))


# --- unit ------------------------------------------------------------------

def test_build_process_normalizes_detail(store):
    _seed(store)
    ai = AIFactoryAdapter("http://ai", transport=_detail_transport())
    out = build_process_detail(store, [ai], "7")
    assert out["available"] is True
    assert out["progress"]["overall_percent"] == 55
    assert out["progress"]["current_subtask"] == "Wire the route"
    assert [s["status"] for s in out["subtasks"]] == ["completed", "in_progress"]


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

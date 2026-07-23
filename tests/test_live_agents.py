"""Tests for live-agent discovery: GET /api/live-agents (#33).

Covers the capability gate, the active/terminal status filter, the cockpit-side
ws_path, and best-effort degradation when AIFactory is unreachable.
"""

from __future__ import annotations

import httpx
from cfactory.adapters import AIFactoryAdapter
from cfactory.app import adapters_dep, create_app
from cfactory.live_agents import _is_active, discover_live_agents
from fastapi.testclient import TestClient


def _router(*, rmux=True, rmux_status=200, tasks=None, tasks_status=200):
    """MockTransport routing /api/capabilities and /api/tasks independently."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/capabilities":
            return httpx.Response(rmux_status, json={"rmux": rmux})
        if request.url.path == "/api/tasks":
            return httpx.Response(tasks_status, json={"tasks": tasks or []})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _ai(**kw) -> AIFactoryAdapter:
    return AIFactoryAdapter("http://ai", transport=_router(**kw))


def _client(adapter: AIFactoryAdapter) -> TestClient:
    app = create_app()
    app.dependency_overrides[adapters_dep] = lambda: [adapter]
    return TestClient(app)


# --- unit: status classification -----------------------------------------


def test_is_active_filters_terminal_statuses():
    assert _is_active("coding") is True
    assert _is_active("planning") is True
    assert _is_active("in_progress") is True
    assert _is_active(None) is True  # just-started agent, no terminal mark yet
    for terminal in ("completed", "merged", "failed", "rejected", "cancelled"):
        assert _is_active(terminal) is False, terminal


def test_is_active_excludes_review_and_queued_statuses():
    # These have no running agent — surfacing them caused consoles that all
    # showed "— stream ended —" (#33).
    for parked in ("human_review", "ai_review", "backlog", "pending", "queued", "todo"):
        assert _is_active(parked) is False, parked


# --- unit: discovery ------------------------------------------------------


def test_discover_returns_only_active_agents_with_proxy_ws_path():
    adapter = _ai(
        rmux=True,
        tasks=[
            {"id": "t1", "status": "coding", "metadata": {"githubIssueNumber": 7}, "title": "A"},
            {"id": "t2", "status": "completed", "metadata": {"githubIssueNumber": 8}},
        ],
    )
    result = discover_live_agents(adapter)
    assert result.rmux_enabled is True
    assert [a.correlation_key for a in result.agents] == ["7"]
    agent = result.agents[0]
    assert agent.spec_id == "t1"
    assert agent.title == "A"
    # Cockpit-side proxy path, never the raw AIFactory URL.
    assert agent.ws_path == "/api/live-agents/7/ws"


def test_discover_gated_off_when_rmux_disabled():
    adapter = _ai(rmux=False, tasks=[{"id": "t1", "status": "coding"}])
    result = discover_live_agents(adapter)
    assert result.rmux_enabled is False
    assert result.agents == []


def test_discover_best_effort_when_capabilities_unreachable():
    adapter = _ai(rmux_status=500)
    result = discover_live_agents(adapter)
    assert result.rmux_enabled is False
    assert result.agents == []


def test_discover_rmux_on_but_tasks_unreachable_yields_no_agents():
    adapter = _ai(rmux=True, tasks_status=500)
    result = discover_live_agents(adapter)
    assert result.rmux_enabled is True
    assert result.agents == []


# --- route ----------------------------------------------------------------


def test_route_returns_envelope_with_count():
    adapter = _ai(
        rmux=True,
        tasks=[{"id": "t1", "status": "coding", "metadata": {"githubIssueNumber": 7}}],
    )
    body = _client(adapter).get("/api/live-agents").json()
    assert body["rmux_enabled"] is True
    assert body["count"] == 1
    assert body["agents"][0]["ws_path"] == "/api/live-agents/7/ws"


def test_route_rmux_off_is_empty_but_ok():
    body = _client(_ai(rmux=False)).get("/api/live-agents").json()
    assert body == {"rmux_enabled": False, "count": 0, "agents": []}


# --- #184: TFactory verify sessions surface as non-streamable rows ----------

from cfactory.adapters import TFactoryAdapter  # noqa: E402
from cfactory.live_agents import discover_tfactory_agents  # noqa: E402
from cfactory.models import Service  # noqa: E402


def _tf(tasks=None, status=200) -> TFactoryAdapter:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tfactory/tasks":
            return httpx.Response(status, json={"tasks": tasks or []})
        return httpx.Response(404, json={})

    return TFactoryAdapter("http://tf", transport=httpx.MockTransport(handle))


def test_discover_tfactory_agents_lists_active_as_non_streamable():
    tf = _tf(
        tasks=[
            {"spec_id": "010-x", "status": "generating", "phase": "gen_functional"},
            {"spec_id": "011-y", "status": "triaged", "phase": "done"},  # terminal → out
        ]
    )
    agents = discover_tfactory_agents(tf)
    assert len(agents) == 1  # only the active (generating) spec
    a = agents[0]
    assert a.service is Service.TFACTORY
    assert a.streamable is False  # no rmux console for TFactory
    assert a.ws_path == ""
    assert a.spec_id == "010-x"
    assert a.phase == "gen_functional"


def test_discover_tfactory_agents_empty_on_error():
    assert discover_tfactory_agents(_tf(status=500)) == []


def test_live_agents_route_unions_aifactory_and_tfactory():
    """The endpoint surfaces BOTH a streamable AIFactory console session and a
    non-streamable TFactory verify session (#184)."""
    ai = _ai(
        rmux=True,
        tasks=[{"id": "ai-1", "status": "coding", "metadata": {"githubIssueNumber": 1}}],
    )
    tf = _tf(tasks=[{"spec_id": "010-x", "status": "evaluating", "phase": "evaluator"}])
    app = create_app()
    app.dependency_overrides[adapters_dep] = lambda: [ai, tf]
    body = TestClient(app).get("/api/live-agents").json()
    assert body["count"] == 2
    services = {a["service"] for a in body["agents"]}
    assert "tfactory" in services
    tf_row = next(a for a in body["agents"] if a["service"] == "tfactory")
    assert tf_row["streamable"] is False and tf_row["ws_path"] == ""

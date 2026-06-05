"""Tests for the live agent console WS proxy (#34).

Covers spec_id resolution, upstream URL derivation, control-frame filtering, and
an end-to-end bridge (fake upstream → cockpit) including read-only behaviour and
clean close on an unknown key.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cfactory.adapters import AIFactoryAdapter
from cfactory.app import adapters_dep, create_app, live_agent_connect_dep
from cfactory.config import Settings
from cfactory.live_agent_proxy import (
    is_control_frame,
    resolve_spec_id,
    upstream_ws_url,
)

CONNECTED = '{"type":"connected","connection_id":"abc"}'
PANE = b"\x1b[32mhello agent\x1b[0m\r\n"


def _router(*, rmux=True, tasks=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/capabilities":
            return httpx.Response(200, json={"rmux": rmux})
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"tasks": tasks or []})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _ai(**kw) -> AIFactoryAdapter:
    return AIFactoryAdapter("http://ai", transport=_router(**kw))


class _FakeUpstream:
    """Async context manager + iterator standing in for a real upstream WS."""

    def __init__(self, frames):
        self._frames = list(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._frames):
            raise StopAsyncIteration
        frame = self._frames[self._i]
        self._i += 1
        return frame


def _fake_connect(frames):
    def _connect(url, **kwargs):
        return _FakeUpstream(frames)

    return _connect


# --- unit -----------------------------------------------------------------

def test_resolve_spec_id_maps_correlation_key_to_task_id():
    adapter = _ai(tasks=[
        {"id": "spec-001", "status": "coding", "metadata": {"githubIssueNumber": 7}},
        {"id": "spec-002", "status": "coding", "metadata": {"githubIssueNumber": 8}},
    ])
    assert resolve_spec_id(adapter, "7") == "spec-001"
    assert resolve_spec_id(adapter, "999") is None


def test_upstream_ws_url_derivation():
    s = Settings(aifactory_api_url="http://localhost:3101")
    assert upstream_ws_url(s, "spec-001") == (
        "ws://localhost:3101/api/tasks/spec-001/agent-console/ws"
    )
    s2 = Settings(aifactory_api_url="https://ai.example.com/")
    assert upstream_ws_url(s2, "x").startswith("wss://ai.example.com/api/tasks/x/")


def test_is_control_frame_only_matches_connected_json():
    assert is_control_frame(CONNECTED) is True
    assert is_control_frame(PANE) is False  # binary pane bytes
    assert is_control_frame("just terminal text") is False
    assert is_control_frame('{"type":"output"}') is False


# --- end-to-end bridge ----------------------------------------------------

def _client(adapter, frames):
    app = create_app()
    app.dependency_overrides[adapters_dep] = lambda: [adapter]
    app.dependency_overrides[live_agent_connect_dep] = lambda: _fake_connect(frames)
    return TestClient(app)


def test_proxy_streams_pane_bytes_and_drops_control_frame():
    adapter = _ai(tasks=[
        {"id": "spec-001", "status": "coding", "metadata": {"githubIssueNumber": 7}},
    ])
    client = _client(adapter, [CONNECTED, PANE])
    with client.websocket_connect("/api/live-agents/7/ws") as ws:
        # The control frame is swallowed; first thing the cockpit sees is pane bytes.
        assert ws.receive_bytes() == PANE


def test_proxy_closes_cleanly_on_unknown_key():
    adapter = _ai(tasks=[
        {"id": "spec-001", "status": "coding", "metadata": {"githubIssueNumber": 7}},
    ])
    client = _client(adapter, [PANE])
    with client.websocket_connect("/api/live-agents/404/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
    assert exc.value.code == 4404


def test_proxy_closes_when_rmux_disabled():
    adapter = _ai(rmux=False, tasks=[
        {"id": "spec-001", "status": "coding", "metadata": {"githubIssueNumber": 7}},
    ])
    client = _client(adapter, [PANE])
    with client.websocket_connect("/api/live-agents/7/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
    assert exc.value.code == 4404

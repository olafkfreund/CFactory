"""Tests for prepared actions (#17) and the propose/execute tools (#18).

Hermetic: no network. The executor and the /api/actions/execute endpoint use an
injected ``httpx.MockTransport``; proposing must make NO HTTP call at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from cfactory.actions import (
    PreparedAction,
    execute_action,
    propose_approve_gate,
    propose_kick_handback,
    propose_trigger_handoff,
)
from cfactory.app import action_transport_dep, create_app, store_dep
from cfactory.models import CompletionEvent, Service
from fastapi.testclient import TestClient


def _seed(store, *, service: Service, correlation_key: str, task_id: str,
          status: str = "done", phase: str | None = None) -> None:
    store.upsert_from_event(
        CompletionEvent(
            correlation_key=correlation_key,
            service=service,
            task_id=task_id,
            status=status,
            phase=phase,
            updated_at=datetime.now(timezone.utc),
        )
    )


class _RecordingTransport(httpx.MockTransport):
    """A MockTransport that records every request it handles."""

    def __init__(self, response: httpx.Response | None = None):
        self.requests: list[httpx.Request] = []
        resp = response or httpx.Response(200, json={"ok": True})

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return resp

        super().__init__(handler)


# --------------------------------------------------------------------------
# propose_* tools build the right PreparedAction
# --------------------------------------------------------------------------

def test_propose_approve_gate_builds_action(store):
    _seed(store, service=Service.PFACTORY, correlation_key="42", task_id="sess-1")
    action = propose_approve_gate(store, "42")
    assert action is not None
    assert action.kind == "approve_gate"
    assert action.correlation_key == "42"
    assert action.target_service == "pfactory"
    assert action.method == "POST"
    assert action.endpoint == "/api/plans/sess-1/approve"
    assert action.payload == {"approver": "cockpit"}
    assert action.rationale


def test_propose_trigger_handoff_builds_action(store):
    _seed(store, service=Service.PFACTORY, correlation_key="42", task_id="sess-1")
    action = propose_trigger_handoff(store, "42")
    assert action is not None
    assert action.kind == "trigger_handoff"
    assert action.target_service == "aifactory"
    assert action.method == "POST"
    assert action.endpoint == "/api/tasks/create-and-run"
    assert action.payload == {"correlation_key": "42", "issue_number": 42}


def test_propose_trigger_handoff_non_numeric_key_has_null_issue(store):
    _seed(store, service=Service.PFACTORY, correlation_key="synthetic-x", task_id="s9")
    action = propose_trigger_handoff(store, "synthetic-x")
    assert action is not None
    assert action.payload == {"correlation_key": "synthetic-x", "issue_number": None}


def test_propose_kick_handback_builds_action(store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    action = propose_kick_handback(store, "42")
    assert action is not None
    assert action.kind == "kick_handback"
    assert action.target_service == "aifactory"
    assert action.method == "POST"
    assert action.endpoint == "/api/tasks/ai-7/apply-correction"
    assert action.payload == {"confirm": True, "source": "cockpit"}


@pytest.mark.parametrize(
    "proposer", [propose_approve_gate, propose_trigger_handoff, propose_kick_handback]
)
def test_propose_returns_none_for_missing_workitem(store, proposer):
    assert proposer(store, "does-not-exist") is None


# --------------------------------------------------------------------------
# /api/actions/propose endpoint
# --------------------------------------------------------------------------

def test_propose_endpoint_returns_action(client, store):
    _seed(store, service=Service.PFACTORY, correlation_key="42", task_id="sess-1")
    resp = client.post(
        "/api/actions/propose", json={"kind": "approve_gate", "correlation_key": "42"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "approve_gate"
    assert body["endpoint"] == "/api/plans/sess-1/approve"
    assert body["target_service"] == "pfactory"


def test_propose_endpoint_404_for_missing_workitem(client):
    resp = client.post(
        "/api/actions/propose", json={"kind": "approve_gate", "correlation_key": "nope"}
    )
    assert resp.status_code == 404


def test_propose_endpoint_400_for_unknown_kind(client, store):
    _seed(store, service=Service.PFACTORY, correlation_key="42", task_id="sess-1")
    resp = client.post(
        "/api/actions/propose", json={"kind": "launch_missiles", "correlation_key": "42"}
    )
    assert resp.status_code == 400


def test_propose_makes_no_http_call(store):
    """Proposing must never touch a service — assert via a transport that would
    record any call. We override the executor transport seam, but propose never
    reaches the executor, so the recorder stays empty."""
    _seed(store, service=Service.PFACTORY, correlation_key="42", task_id="sess-1")
    recorder = _RecordingTransport()
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[action_transport_dep] = lambda: recorder
    api = TestClient(app)

    resp = api.post(
        "/api/actions/propose", json={"kind": "approve_gate", "correlation_key": "42"}
    )
    assert resp.status_code == 200
    assert recorder.requests == []  # no service was contacted


# --------------------------------------------------------------------------
# execute_action / /api/actions/execute
# --------------------------------------------------------------------------

def test_execute_action_hits_correct_url_and_method():
    recorder = _RecordingTransport(httpx.Response(201, json={"approved": True}))
    action = PreparedAction(
        kind="approve_gate",
        correlation_key="42",
        target_service="pfactory",
        method="POST",
        endpoint="/api/plans/sess-1/approve",
        payload={"approver": "cockpit"},
        rationale="why",
    )
    result = execute_action(action, transport=recorder)

    assert result == {"status_code": 201, "ok": True, "body": {"approved": True}}
    assert len(recorder.requests) == 1
    req = recorder.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "http://localhost:3102/api/plans/sess-1/approve"


def test_execute_action_swallows_transport_errors():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    action = PreparedAction(
        kind="trigger_handoff",
        correlation_key="42",
        target_service="aifactory",
        method="POST",
        endpoint="/api/tasks/create-and-run",
        payload={},
        rationale="why",
    )
    result = execute_action(action, transport=httpx.MockTransport(boom))
    assert result["status_code"] == 0
    assert result["ok"] is False
    assert "error" in result


def test_execute_action_unknown_target_service():
    action = PreparedAction(
        kind="approve_gate",
        correlation_key="42",
        target_service="bogus",
        method="POST",
        endpoint="/x",
        payload={},
        rationale="why",
    )
    result = execute_action(action)
    assert result["status_code"] == 0
    assert result["ok"] is False


def test_execute_endpoint_runs_confirmed_action(client_with_transport):
    api, recorder = client_with_transport
    body = {
        "kind": "trigger_handoff",
        "correlation_key": "42",
        "target_service": "aifactory",
        "method": "POST",
        "endpoint": "/api/tasks/create-and-run",
        "payload": {"correlation_key": "42", "issue_number": 42},
        "rationale": "hand off",
    }
    resp = api.post("/api/actions/execute", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"status_code": 200, "ok": True, "body": {"ok": True}}
    assert len(recorder.requests) == 1
    req = recorder.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "http://localhost:3101/api/tasks/create-and-run"


@pytest.fixture
def client_with_transport(store):
    recorder = _RecordingTransport()
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[action_transport_dep] = lambda: recorder
    return TestClient(app), recorder

"""Tests for prepared actions and the propose/execute tools.

Hermetic: no network. The executor and the /api/actions/execute endpoint use an
injected ``httpx.MockTransport``; proposing must make NO HTTP call at all.

Action kinds map to the factories' real task API:
  approve_plan   → POST /api/tasks/{id}/approve-plan
  approve_review → POST /api/tasks/{id}/worktree/create-pr  then  …/merge
  reject_review  → POST /api/tasks/{id}/apply-correction  {reason}
  recover        → POST /api/tasks/{id}/recover
  delete_task    → DELETE /api/tasks/{id}
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from cfactory.actions import (
    PreparedAction,
    execute_action,
    is_safe_endpoint,
    propose_approve_plan,
    propose_approve_review,
    propose_delete_task,
    propose_recover,
    propose_reject_review,
)
from cfactory.app import action_transport_dep, audit_dep, create_app, store_dep
from cfactory.audit import AuditStore
from cfactory.config import Settings
from cfactory.models import CompletionEvent, Service
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _seed(store, *, service: Service, correlation_key: str, task_id: str,
          status: str = "human_review", phase: str | None = None) -> None:
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

def test_propose_approve_plan(store):
    _seed(store, service=Service.PFACTORY, correlation_key="42", task_id="sess-1", status="planned")
    action = propose_approve_plan(store, "42")
    assert action is not None
    assert action.kind == "approve_plan"
    assert action.target_service == "pfactory"
    assert action.endpoint == "/api/tasks/sess-1/approve-plan"
    assert action.payload == {"approver": "cockpit"}


def test_propose_approve_review_creates_pr_then_merges(store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    action = propose_approve_review(store, "42")
    assert action is not None
    assert action.kind == "approve_review"
    assert action.target_service == "aifactory"
    assert action.method == "POST"
    assert action.endpoint == "/api/tasks/ai-7/worktree/create-pr"
    assert len(action.follow_ups) == 1
    assert action.follow_ups[0].endpoint == "/api/tasks/ai-7/worktree/merge"


def test_propose_reject_review_carries_reason(store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    action = propose_reject_review(store, "42", note="missing tests")
    assert action is not None
    assert action.kind == "reject_review"
    assert action.endpoint == "/api/tasks/ai-7/apply-correction"
    assert action.payload == {"reason": "missing tests", "source": "cockpit"}


def test_propose_reject_review_defaults_reason(store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    action = propose_reject_review(store, "42", note="   ")
    assert action.payload["reason"] == "Rejected from the cockpit."


def test_propose_recover(store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7", status="in_progress")
    action = propose_recover(store, "42")
    assert action is not None
    assert action.endpoint == "/api/tasks/ai-7/recover"


def test_propose_delete_task_uses_delete_verb(store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    action = propose_delete_task(store, "42")
    assert action is not None
    assert action.method == "DELETE"
    assert action.endpoint == "/api/tasks/ai-7"


# --------------------------------------------------------------------------
# PFactory plan-stage routing (#bug: plan sessions use /api/plan/sessions/{id}/*,
# not the AIFactory-style /api/tasks/{id} surface — and have no delete)
# --------------------------------------------------------------------------

def _seed_plan(store, key="013-probe2"):
    _seed(store, service=Service.PFACTORY, correlation_key=key, task_id=key,
          status="human_review", phase="plan")


def test_propose_approve_review_plan_uses_session_approve(store):
    _seed_plan(store)
    action = propose_approve_review(store, "013-probe2")
    assert action.target_service == "pfactory"
    assert action.method == "POST"
    assert action.endpoint == "/api/plan/sessions/013-probe2/approve"
    assert action.payload["approver"] == "cockpit"
    assert action.follow_ups == []  # no PR/merge for a plan


def test_propose_reject_review_plan_uses_session_reject(store):
    _seed_plan(store)
    action = propose_reject_review(store, "013-probe2", note="needs scope cut")
    assert action.endpoint == "/api/plan/sessions/013-probe2/reject"
    assert action.payload == {"approver": "cockpit", "feedback": "needs scope cut"}


def test_propose_recover_plan_uses_session_process(store):
    _seed_plan(store)
    action = propose_recover(store, "013-probe2")
    assert action.endpoint == "/api/plan/sessions/013-probe2/process"


def test_propose_delete_task_unsupported_for_plan(store):
    _seed_plan(store)
    # PFactory has no session delete — "remove" must not be proposable for a plan.
    assert propose_delete_task(store, "013-probe2") is None


def test_review_target_prefers_furthest_nonterminal_stage(store):
    # pfactory done (terminal) + aifactory in review → target aifactory.
    _seed(store, service=Service.PFACTORY, correlation_key="42", task_id="p1", status="done")
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7", status="human_review")
    action = propose_approve_review(store, "42")
    assert action.target_service == "aifactory"


@pytest.mark.parametrize(
    "proposer",
    [propose_approve_review, propose_reject_review, propose_recover, propose_delete_task],
)
def test_propose_returns_none_for_missing_workitem(store, proposer):
    assert proposer(store, "does-not-exist") is None


# --------------------------------------------------------------------------
# /api/actions/propose endpoint
# --------------------------------------------------------------------------

def test_propose_endpoint_returns_action(client, store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    resp = client.post(
        "/api/actions/propose", json={"kind": "approve_review", "correlation_key": "42"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "approve_review"
    assert body["endpoint"] == "/api/tasks/ai-7/worktree/create-pr"


def test_propose_endpoint_passes_note(client, store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    resp = client.post(
        "/api/actions/propose",
        json={"kind": "reject_review", "correlation_key": "42", "note": "no"},
    )
    assert resp.status_code == 200
    assert resp.json()["payload"]["reason"] == "no"


def test_propose_endpoint_404_for_missing_workitem(client):
    resp = client.post(
        "/api/actions/propose", json={"kind": "approve_review", "correlation_key": "nope"}
    )
    assert resp.status_code == 404


def test_propose_endpoint_400_for_unknown_kind(client, store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    resp = client.post(
        "/api/actions/propose", json={"kind": "launch_missiles", "correlation_key": "42"}
    )
    assert resp.status_code == 400


def test_propose_makes_no_http_call(store):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    recorder = _RecordingTransport()
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[action_transport_dep] = lambda: recorder
    api = TestClient(app)

    resp = api.post(
        "/api/actions/propose", json={"kind": "approve_review", "correlation_key": "42"}
    )
    assert resp.status_code == 200
    assert recorder.requests == []  # no service was contacted


# --------------------------------------------------------------------------
# execute_action / /api/actions/execute
# --------------------------------------------------------------------------

def test_execute_action_hits_correct_url_and_method():
    recorder = _RecordingTransport(httpx.Response(201, json={"approved": True}))
    action = PreparedAction(
        kind="approve_plan", correlation_key="42", target_service="pfactory",
        method="POST", endpoint="/api/tasks/sess-1/approve-plan",
        payload={"approver": "cockpit"}, rationale="why",
    )
    result = execute_action(action, transport=recorder)
    assert result["status_code"] == 201
    assert result["ok"] is True
    assert result["body"] == {"approved": True}
    assert len(recorder.requests) == 1
    assert str(recorder.requests[0].url) == "http://localhost:3105/api/tasks/sess-1/approve-plan"


def test_execute_action_sends_bearer_token():
    recorder = _RecordingTransport()
    action = PreparedAction(
        kind="recover", correlation_key="42", target_service="aifactory",
        method="POST", endpoint="/api/tasks/ai-7/recover", payload={}, rationale="why",
    )
    execute_action(action, settings=Settings(upstream_token="sekret"), transport=recorder)
    assert recorder.requests[0].headers.get("authorization") == "Bearer sekret"


def test_execute_action_runs_follow_ups_in_order():
    recorder = _RecordingTransport()
    action = PreparedAction(
        kind="approve_review", correlation_key="42", target_service="aifactory",
        method="POST", endpoint="/api/tasks/ai-7/worktree/create-pr", payload={},
        follow_ups=[{"method": "POST", "endpoint": "/api/tasks/ai-7/worktree/merge", "payload": {}}],
        rationale="approve",
    )
    result = execute_action(action, transport=recorder)
    assert result["ok"] is True
    assert len(recorder.requests) == 2
    assert recorder.requests[0].url.path.endswith("/worktree/create-pr")
    assert recorder.requests[1].url.path.endswith("/worktree/merge")
    assert len(result["steps"]) == 2


def test_execute_action_stops_chain_on_first_failure():
    # create-pr fails → merge must NOT run.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    action = PreparedAction(
        kind="approve_review", correlation_key="42", target_service="aifactory",
        method="POST", endpoint="/api/tasks/ai-7/worktree/create-pr", payload={},
        follow_ups=[{"method": "POST", "endpoint": "/api/tasks/ai-7/worktree/merge"}],
        rationale="approve",
    )
    recorder = _RecordingTransport()
    recorder._handler = handler  # type: ignore[attr-defined]
    result = execute_action(action, transport=httpx.MockTransport(handler))
    assert result["ok"] is False
    assert len(result["steps"]) == 1  # merge never attempted


def test_execute_action_swallows_transport_errors():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    action = PreparedAction(
        kind="recover", correlation_key="42", target_service="aifactory",
        method="POST", endpoint="/api/tasks/ai-7/recover", payload={}, rationale="why",
    )
    result = execute_action(action, transport=httpx.MockTransport(boom))
    assert result["status_code"] == 0
    assert result["ok"] is False
    assert "error" in result


def test_execute_action_unknown_target_service():
    action = PreparedAction(
        kind="recover", correlation_key="42", target_service="bogus",
        method="POST", endpoint="/api/tasks/x/recover", payload={}, rationale="why",
    )
    result = execute_action(action)
    assert result["status_code"] == 0
    assert result["ok"] is False


def test_execute_endpoint_runs_confirmed_action(client_with_transport):
    api, recorder = client_with_transport
    body = {
        "kind": "delete_task", "correlation_key": "42", "target_service": "aifactory",
        "method": "DELETE", "endpoint": "/api/tasks/ai-7", "payload": {}, "rationale": "remove",
    }
    resp = api.post("/api/actions/execute", json=body)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(recorder.requests) == 1
    assert recorder.requests[0].method == "DELETE"
    assert str(recorder.requests[0].url) == "http://localhost:3101/api/tasks/ai-7"


# --------------------------------------------------------------------------
# SSRF guard: endpoint must be a root-relative path; method allow-listed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "http://evil.example.com/steal",
    "https://169.254.169.254/latest/meta-data/",
    "//evil.example.com/x",
    "ftp://host/x",
    "not-a-path",
])
def test_is_safe_endpoint_rejects_absolute_and_protocol_relative(bad):
    assert is_safe_endpoint(bad) is False


@pytest.mark.parametrize("ok", ["/api/x", "/api/tasks/1/approve-plan", "/"])
def test_is_safe_endpoint_allows_root_relative(ok):
    assert is_safe_endpoint(ok) is True


def test_prepared_action_rejects_absolute_endpoint():
    with pytest.raises(ValidationError):
        PreparedAction(kind="recover", correlation_key="1", target_service="aifactory",
                       method="POST", endpoint="http://169.254.169.254/x", payload={}, rationale="x")


def test_prepared_action_rejects_bad_method():
    with pytest.raises(ValidationError):
        PreparedAction(kind="recover", correlation_key="1", target_service="aifactory",
                       method="CONNECT", endpoint="/api/x", payload={}, rationale="x")


def test_execute_endpoint_rejects_absolute_endpoint_and_makes_no_call(client_with_transport):
    api, recorder = client_with_transport
    body = {
        "kind": "recover", "correlation_key": "42", "target_service": "aifactory",
        "method": "GET", "endpoint": "http://169.254.169.254/latest/meta-data/",
        "payload": {}, "rationale": "x",
    }
    resp = api.post("/api/actions/execute", json=body)
    assert resp.status_code == 422
    assert recorder.requests == []  # no SSRF — nothing was contacted


def test_execute_action_guard_blocks_absolute_endpoint():
    recorder = _RecordingTransport()
    action = PreparedAction.model_construct(
        kind="recover", correlation_key="1", target_service="aifactory",
        method="GET", endpoint="http://169.254.169.254/x", payload={}, follow_ups=[], rationale="x",
    )
    result = execute_action(action, transport=recorder)
    assert result["ok"] is False
    assert recorder.requests == []


@pytest.fixture
def client_with_transport(store, tmp_path):
    recorder = _RecordingTransport()
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[action_transport_dep] = lambda: recorder
    audit = AuditStore(f"sqlite:///{tmp_path / 'audit.db'}")
    app.dependency_overrides[audit_dep] = lambda: audit
    return TestClient(app), recorder

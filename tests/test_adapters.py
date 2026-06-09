"""Tests for the service adapters (#7-#9) using a mock HTTP transport."""

from __future__ import annotations

import httpx
import pytest

from cfactory.adapters import (
    AIFactoryAdapter,
    AdapterError,
    PFactoryAdapter,
    TFactoryAdapter,
    hydrate,
)
from cfactory.models import Service


def _transport(payload, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


def test_adapter_sends_bearer_token_when_configured():
    seen: dict[str, str | None] = {}

    def capture(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"tasks": []})

    adapter = AIFactoryAdapter(
        "http://x", token="sekret", transport=httpx.MockTransport(capture)
    )
    adapter.list_items()
    assert seen["auth"] == "Bearer sekret"


def test_adapter_sends_no_auth_header_without_token():
    seen: dict[str, str | None] = {}

    def capture(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"tasks": []})

    AIFactoryAdapter("http://x", transport=httpx.MockTransport(capture)).list_items()
    assert seen["auth"] is None


def test_pfactory_hits_plan_sessions_path_and_reads_sessions_envelope():
    seen: dict[str, str] = {}

    def capture(request):
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={"sessions": [{"session_id": "s1", "board_state": "human_review",
                                "github_issue": 42, "title": "Login plan"}]},
        )

    items = PFactoryAdapter("http://x", transport=httpx.MockTransport(capture)).list_items()
    assert seen["path"] == "/api/plan/sessions"   # not the old /api/plans (404)
    assert len(items) == 1                          # {"sessions": [...]} envelope is unwrapped
    assert items[0].correlation_key == "42"
    assert items[0].task_id == "s1"
    assert items[0].status == "human_review"


def test_health_true_when_service_responds():
    # Any HTTP response — even a 404 — means the process is up.
    adapter = AIFactoryAdapter("http://x", transport=_transport({}, status=404))
    assert adapter.health() is True


def test_health_false_on_connect_error():
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    adapter = AIFactoryAdapter("http://x", transport=httpx.MockTransport(boom))
    assert adapter.health() is False


def test_aifactory_normalizes_and_extracts_issue_key():
    payload = {"tasks": [{"id": "t1", "status": "coding", "phase": "code",
                          "metadata": {"githubIssueNumber": 42}, "title": "Login"}]}
    adapter = AIFactoryAdapter("http://x", transport=_transport(payload))
    items = adapter.list_items()
    assert len(items) == 1
    it = items[0]
    assert it.correlation_key == "42"      # from metadata.githubIssueNumber
    assert it.service is Service.AIFACTORY
    assert it.task_id == "t1"
    assert it.status == "coding"
    assert it.phase == "code"


def test_pfactory_accepts_bare_list_and_board_state():
    payload = [{"session_id": "s1", "board_state": "human_review", "github_issue": 42,
                "title": "Login plan"}]
    items = PFactoryAdapter("http://x", transport=_transport(payload)).list_items()
    assert items[0].correlation_key == "42"
    assert items[0].task_id == "s1"
    assert items[0].status == "human_review"
    assert items[0].phase == "plan"


def test_tfactory_reads_nested_provenance():
    payload = {"items": [{"spec_id": "spec1", "status": "triaged", "phase": "test",
                          "source": {"aifactory": {"github_issue": 42}}}]}
    items = TFactoryAdapter("http://x", transport=_transport(payload)).list_items()
    assert items[0].correlation_key == "42"
    assert items[0].task_id == "spec1"


def test_correlation_key_falls_back_to_task_id_when_no_issue():
    payload = {"tasks": [{"id": "t9", "status": "done"}]}
    items = AIFactoryAdapter("http://x", transport=_transport(payload)).list_items()
    assert items[0].correlation_key == "t9"


def test_http_error_raises_adapter_error():
    adapter = AIFactoryAdapter("http://x", transport=_transport({"e": 1}, status=500))
    with pytest.raises(AdapterError):
        adapter.list_items()


def test_hydrate_threads_services_by_correlation_key(store):
    pf = PFactoryAdapter("http://x", transport=_transport(
        [{"session_id": "s1", "board_state": "done", "github_issue": 42, "title": "Login"}]))
    ai = AIFactoryAdapter("http://x", transport=_transport(
        {"tasks": [{"id": "t1", "status": "coding", "metadata": {"githubIssueNumber": 42}}]}))

    n = hydrate(store, pf.list_items()) + hydrate(store, ai.list_items())
    assert n == 2

    wi = store.get("42")
    assert wi is not None
    assert wi.pfactory.status == "done"
    assert wi.aifactory.status == "coding"
    assert wi.title == "Login"          # set from the first item that carried one
    assert wi.timeline == []            # snapshots don't append events

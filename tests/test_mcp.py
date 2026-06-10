"""Tests for the read-only CFactory MCP server (POST /mcp, JSON-RPC 2.0).

The MCP surface is the single PARR-pipeline visibility plane for external agents
(Claude Code, the /parr-run conductor). It must: enforce the bearer secret,
speak initialize/tools/list/tools/call, and return the same cross-factory state
the REST cockpit shows — sourced from the shared WorkItemStore.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cfactory import mcp
from cfactory.app import create_app
from cfactory.models import CompletionEvent, Service


@pytest.fixture
def seeded_store(store):
    # Thread one unit of work across plan -> code so the tools have data.
    from datetime import datetime, timezone

    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="142", service=Service.PFACTORY,
            task_id="plan-1", status="done", updated_at=now,
        )
    )
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="142", service=Service.AIFACTORY,
            task_id="010-x", status="in_progress", phase="coding", updated_at=now,
        )
    )
    return store


@pytest.fixture
def mcp_client(seeded_store, monkeypatch):
    # The MCP handlers call get_store() directly (not via Depends), so point that
    # at the hermetic fixture store.
    monkeypatch.setattr(mcp, "get_store", lambda: seeded_store)
    monkeypatch.setenv("CFACTORY_MCP_SECRET", "test-secret")
    return TestClient(create_app())


AUTH = {"Authorization": "Bearer test-secret"}


def _call(client, name, arguments=None):
    return client.post(
        "/mcp", headers=AUTH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments or {}}},
    )


def test_auth_required(mcp_client):
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_initialize_and_tools_list(mcp_client):
    r = mcp_client.post("/mcp", headers=AUTH,
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.json()["result"]["serverInfo"]["name"] == "cfactory"

    r = mcp_client.post("/mcp", headers=AUTH,
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "cfactory_list_workitems", "cfactory_get_workitem", "cfactory_get_timeline",
        "cfactory_get_rollups", "cfactory_get_anomalies",
    }


def test_list_workitems_summarizes_each_factory(mcp_client):
    import json

    r = _call(mcp_client, "cfactory_list_workitems")
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["count"] == 1
    row = payload["items"][0]
    assert row["correlation_key"] == "142"
    assert row["pfactory"]["status"] == "done"
    assert row["aifactory"]["status"] == "in_progress"
    assert row["aifactory"]["phase"] == "coding"


def test_get_workitem_full_state(mcp_client):
    import json

    r = _call(mcp_client, "cfactory_get_workitem", {"correlation_key": "142"})
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["correlation_key"] == "142"
    assert payload["aifactory"]["phase"] == "coding"


def test_get_workitem_missing_key(mcp_client):
    import json

    r = _call(mcp_client, "cfactory_get_workitem", {"correlation_key": "999"})
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "error" in payload


def test_unknown_method_is_jsonrpc_error(mcp_client):
    r = mcp_client.post("/mcp", headers=AUTH,
                        json={"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32601

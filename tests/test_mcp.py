"""Tests for the read-only CFactory MCP server (POST /mcp, JSON-RPC 2.0).

The MCP surface is the single PARR-pipeline visibility plane for external agents
(Claude Code, the /parr-run conductor). It must: enforce the bearer secret,
speak initialize/tools/list/tools/call, and return the same cross-factory state
the REST cockpit shows — sourced from the shared WorkItemStore.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cfactory import auth, config, mcp
from cfactory.app import create_app
from cfactory.models import CompletionEvent, Service


@pytest.fixture
def seeded_store(store):
    # Thread one unit of work across plan -> code so the tools have data.
    from datetime import datetime, timezone

    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="142",
            service=Service.PFACTORY,
            task_id="plan-1",
            status="done",
            updated_at=now,
        )
    )
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="142",
            service=Service.AIFACTORY,
            task_id="010-x",
            status="in_progress",
            phase="coding",
            updated_at=now,
        )
    )
    return store


@pytest.fixture
def mcp_client(seeded_store, monkeypatch):
    # The MCP handlers call get_store() directly (not via Depends), so point that
    # at the hermetic fixture store.
    monkeypatch.setattr(mcp, "get_store", lambda: seeded_store)
    # The MCP secret now flows through the typed Settings boundary (#113), so set
    # the env var AND drop the cached Settings singleton so get_settings() rebuilds
    # from the patched environment.
    monkeypatch.setenv("CFACTORY_MCP_SECRET", "test-secret")
    monkeypatch.delenv("CFACTORY_API_KEYS", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.reset_keystore()  # /mcp now consults the scoped keystore too
    yield TestClient(create_app())
    auth.reset_keystore()


AUTH = {"Authorization": "Bearer test-secret"}


def _call(client, name, arguments=None):
    return client.post(
        "/mcp",
        headers=AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )


def test_auth_required(mcp_client):
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_initialize_and_tools_list(mcp_client):
    r = mcp_client.post(
        "/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert r.json()["result"]["serverInfo"]["name"] == "cfactory"

    r = mcp_client.post(
        "/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "cfactory_list_workitems",
        "cfactory_get_workitem",
        "cfactory_get_timeline",
        "cfactory_get_rollups",
        "cfactory_get_anomalies",
        # RFC-0019 Phase 2b board tools.
        "cfactory_list_cards",
        "cfactory_get_card",
        # The imported issue discussion (Factory#375).
        "cfactory_card_comments",
        "cfactory_create_card",
        # RFC-0019 Phase 6 GitHub sync.
        "cfactory_sync_card_github",
        # RFC-0020 Phase 6 issue import, and #374's staleness read beside it.
        "cfactory_import_cards",
        "cfactory_card_sync_state",
        "cfactory_update_card",
        "cfactory_move_card",
        "cfactory_reprioritise_card",
        # Explicit stage actions (RFC-0020 §3.7) — each the twin of
        # POST /api/cards/{key}/actions/<stage>.
        "cfactory_plan_card",
        "cfactory_code_card",
        "cfactory_test_card",
        "cfactory_run_card",
        "cfactory_delete_card",
        # Tenant git configuration (RFC-0020 §3.3) — the twins of
        # GET/PUT /api/tenants/{tenant}/git-config and its :verify.
        "cfactory_get_git_config",
        "cfactory_set_git_config",
        "cfactory_verify_git_config",
        # Tenant git credential (RFC-0020 §3.4) — write-only, so there is a set
        # and a delete and deliberately no read.
        "cfactory_set_git_credential",
        "cfactory_delete_git_credential",
        # Git connections and repositories (RFC-0020 §3.3 phase 8) — many hosts,
        # many repos per host, and the tenant default a card falls back to.
        "cfactory_list_git_connections",
        "cfactory_create_git_connection",
        "cfactory_update_git_connection",
        "cfactory_delete_git_connection",
        "cfactory_verify_git_connection",
        "cfactory_set_git_connection_credential",
        "cfactory_delete_git_connection_credential",
        "cfactory_list_git_repositories",
        "cfactory_create_git_repository",
        "cfactory_update_git_repository",
        "cfactory_delete_git_repository",
        "cfactory_set_default_git_repository",
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
    r = mcp_client.post("/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32601


# ── Scope model (RFC-0019 Phase 2a) ──────────────────────────────────────────
# /mcp used to fail OPEN when no secret was set. Phase 2b hangs board WRITE tools
# off this transport, so: unconfigured must deny, scoped keys gate per tool, and
# the legacy CFACTORY_MCP_SECRET must keep working untouched (it is live in prod).


@pytest.fixture
def scoped_client(seeded_store, monkeypatch):
    """Client with NO legacy secret — only the scoped CFACTORY_API_KEYS keystore."""
    monkeypatch.setattr(mcp, "get_store", lambda: seeded_store)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.setenv("CFACTORY_API_KEYS", "reader-key:read;writer-key:read,write")
    monkeypatch.setattr(config, "_settings", None)
    auth.reset_keystore()
    yield TestClient(create_app())
    auth.reset_keystore()


def _call_as(client, token, name, arguments=None):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )


def test_read_tool_allowed_for_read_scoped_key(scoped_client):
    r = _call_as(scoped_client, "reader-key", "cfactory_list_workitems")
    assert r.status_code == 200
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["count"] == 1


def test_write_tool_refused_to_read_only_key(scoped_client, monkeypatch):
    # No write tools exist yet (Phase 2b adds them), so register a dummy one for
    # the duration of this test to exercise the real enforcement path.
    monkeypatch.setitem(mcp.TOOL_SCOPES, "cfactory_dummy_write", auth.WRITE)
    monkeypatch.setitem(mcp._TOOL_HANDLERS, "cfactory_dummy_write", lambda _a, _c: {"ok": True})

    r = _call_as(scoped_client, "reader-key", "cfactory_dummy_write")
    assert r.status_code == 403
    assert "write" in r.json()["detail"]

    # ...and the same tool works for a key that does hold write.
    r = _call_as(scoped_client, "writer-key", "cfactory_dummy_write")
    assert r.status_code == 200
    assert json.loads(r.json()["result"]["content"][0]["text"]) == {"ok": True}


def test_unregistered_tool_requires_write(scoped_client):
    """A tool with no TOOL_SCOPES entry fails closed rather than inheriting read."""
    r = _call_as(scoped_client, "reader-key", "cfactory_not_registered")
    assert r.status_code == 403


def test_unknown_key_is_rejected(scoped_client):
    assert _call_as(scoped_client, "nope", "cfactory_get_rollups").status_code == 401


def test_legacy_mcp_secret_is_full_scope(mcp_client, monkeypatch):
    """The live prod credential keeps working — and counts as read AND write."""
    monkeypatch.setitem(mcp.TOOL_SCOPES, "cfactory_dummy_write", auth.WRITE)
    monkeypatch.setitem(mcp._TOOL_HANDLERS, "cfactory_dummy_write", lambda _a, _c: {"ok": True})

    assert _call_as(mcp_client, "test-secret", "cfactory_get_rollups").status_code == 200
    assert _call_as(mcp_client, "test-secret", "cfactory_dummy_write").status_code == 200


def test_unconfigured_denies_instead_of_opening(seeded_store, monkeypatch):
    """No secret, no keys, no dev opt-in => 401. This used to be wide open."""
    monkeypatch.setattr(mcp, "get_store", lambda: seeded_store)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.delenv("CFACTORY_API_KEYS", raising=False)
    monkeypatch.delenv("CFACTORY_MCP_DEV_OPEN", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.reset_keystore()
    try:
        client = TestClient(create_app())
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert "not configured" in r.json()["detail"]
    finally:
        auth.reset_keystore()


def test_dev_open_flag_restores_open_mode(seeded_store, monkeypatch):
    """The explicit local-dev escape hatch — unconfigured but opted in."""
    monkeypatch.setattr(mcp, "get_store", lambda: seeded_store)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.delenv("CFACTORY_API_KEYS", raising=False)
    monkeypatch.setenv("CFACTORY_MCP_DEV_OPEN", "true")
    monkeypatch.setattr(config, "_settings", None)
    auth.reset_keystore()
    try:
        client = TestClient(create_app())
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200
    finally:
        auth.reset_keystore()

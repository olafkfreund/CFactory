"""MCP investigation authorization, switch behavior, and worker execution."""

from __future__ import annotations

import asyncio
import json
from threading import Event
from time import monotonic

import httpx
import pytest
from cfactory import auth, config, mcp
from cfactory.app import create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from cfactory.git_connections import GitConnectionCreate, GitRepositoryCreate
from fastapi.testclient import TestClient

_READER = "reader"
_WRITER = "writer"
_LEGACY_TOKEN = "legacy-secret"  # noqa: S105 — test credential
_SHA = "a" * 40
_NAMES = {tool["name"] for tool in mcp.INVESTIGATION_TOOLS}
_WRITES = {
    "cfactory_create_investigation_card": {"request": "why?", "repository_id": 1},
    "cfactory_investigate_card": {
        "card_key": "FCT-1",
        "request": "why?",
        "sha": _SHA,
        "test_command": None,
    },
    "cfactory_investigation_lease": {"card_key": "FCT-1", "run_id": "run-1"},
    "cfactory_investigation_stop": {"card_key": "FCT-1", "run_id": "run-1"},
}


class Upstream:
    def __init__(self) -> None:
        self.entered: Event | None = None
        self.release: Event | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/widgets":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": _SHA})
        if "/compare/" in path:
            return httpx.Response(200, json={"status": "identical"})
        if path == "/api/investigations" and request.method == "POST":
            if self.entered and self.release:
                self.entered.set()
                self.release.wait(5)
            return httpx.Response(200, json={"id": "run-1", "state": "running"})
        return httpx.Response(200, json={"state": "running", "pods_gone": False})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CFACTORY_INVESTIGATIONS", "on")
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.delenv("CFACTORY_MCP_DEV_OPEN", raising=False)
    config.reset_settings()
    auth.set_keys({_READER: {"read"}, _WRITER: {"read", "write"}})
    store = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    audit = AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="mcp-inv-test")  # noqa: S106
    upstream = Upstream()
    connection = store.create_connection(GitConnectionCreate(provider="github"))
    repo = store.create_repository(
        connection.id, GitRepositoryCreate(project="acme/widgets", aifactory_project_id="proj-1")
    )
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: store)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", lambda: httpx.MockTransport(upstream))
    app = create_app()
    yield app, store, upstream, repo.id
    auth.reset_keystore()
    config.reset_settings()


def _call(client, name, args=None, *, token=_WRITER):
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        },
    )
    return response, json.loads(
        response.json()["result"]["content"][0]["text"]
    ) if response.status_code == 200 else None


def _names(client, *, token=_WRITER):
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def test_read_key_lacks_scope_for_every_write(env):
    client = TestClient(env[0])
    for name, args in _WRITES.items():
        response, _ = _call(client, name, args, token=_READER)
        assert response.status_code == 403


def test_dev_open_never_authorizes_investigation_writes(env, monkeypatch):
    client = TestClient(env[0])
    auth.reset_keystore()
    monkeypatch.setenv("CFACTORY_MCP_DEV_OPEN", "true")
    config.reset_settings()
    for name, args in _WRITES.items():
        _, payload = _call(client, name, args, token=None)
        assert payload["reason"] == "keys_required"


def test_legacy_secret_accepts_write(env, monkeypatch):
    client, _, _, repo_id = TestClient(env[0]), *env[1:]
    monkeypatch.setenv("CFACTORY_MCP_SECRET", _LEGACY_TOKEN)
    config.reset_settings()
    _, payload = _call(
        client,
        "cfactory_create_investigation_card",
        {"request": "why?", "repository_id": repo_id},
        token=_LEGACY_TOKEN,
    )
    assert payload["card_key"] == "FCT-1"


def test_multi_tenant_refuses_every_tool(env, monkeypatch):
    client = TestClient(env[0])
    monkeypatch.setenv("CFACTORY_MULTI_TENANT", "true")
    config.reset_settings()
    for name in _NAMES:
        _, payload = _call(client, name, _WRITES.get(name, {}))
        assert payload["reason"] == "multi_tenant_unsupported"


def test_invalid_arguments_do_not_raise_internal_error(env):
    client = TestClient(env[0])
    _, payload = _call(client, "cfactory_create_investigation_card", ["bad"])
    assert payload["reason"] == "invalid_arguments"


def test_switch_filters_list_and_direct_calls(env, monkeypatch):
    client = TestClient(env[0])
    assert _NAMES.issubset(_names(client))
    monkeypatch.setenv("CFACTORY_INVESTIGATIONS", "drain")
    config.reset_settings()
    assert (_NAMES - mcp._INVESTIGATION_DRAIN_HIDDEN).issubset(_names(client))
    assert not mcp._INVESTIGATION_DRAIN_HIDDEN & _names(client)
    for name in mcp._INVESTIGATION_DRAIN_HIDDEN:
        _, payload = _call(client, name, _WRITES[name])
        assert payload["reason"] == "investigations_draining"
    monkeypatch.setenv("CFACTORY_INVESTIGATIONS", "off")
    config.reset_settings()
    assert not _NAMES & _names(client)
    response, payload = _call(
        client, "cfactory_investigate_card", _WRITES["cfactory_investigate_card"], token=_READER
    )
    assert response.status_code == 200
    assert payload == {"error": "unknown tool: cfactory_investigate_card"}


@pytest.mark.asyncio
async def test_blocked_dispatch_does_not_block_mcp(env):
    app, store, upstream, repo_id = env
    store.create_investigation_card(
        title="Investigate: why?",
        description="why?",
        repository_id=repo_id,
        binding={
            "repository_id": repo_id,
            "repo": "acme/widgets",
            "aifactory_project_id": "proj-1",
        },
    )
    upstream.entered, upstream.release = Event(), Event()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def call(method, params):
            return await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {_WRITER}"},
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )

        started = monotonic()
        dispatch = asyncio.create_task(
            call(
                "tools/call",
                {
                    "name": "cfactory_investigate_card",
                    "arguments": _WRITES["cfactory_investigate_card"],
                },
            )
        )
        try:
            assert await asyncio.to_thread(upstream.entered.wait, 5)
            listed = await asyncio.wait_for(call("tools/list", {}), timeout=2)
            assert listed.status_code == 200
            assert monotonic() - started < 4
        finally:
            upstream.release.set()
        assert (await dispatch).status_code == 200

"""Tests for the agent capability manifest (RFC-0019 §3.4).

Two properties matter: the shape is what a discovering agent expects, and it is
readable WITHOUT a credential — agents enumerate capabilities before they hold a
token. The second is asserted with the keystore ENFORCED (the rest of the suite
runs OPEN, where every path is reachable and would prove nothing).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cfactory import __version__
from cfactory.app import create_app, store_dep
from cfactory.auth import reset_keystore, set_keys
from cfactory.mcp import MCP_TOOLS
from cfactory.routes_well_known import WELL_KNOWN_AGENT_SKILLS_PATH
from cfactory.store import WorkItemStore


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def client(store):
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    return TestClient(app)


def test_manifest_shape(client):
    resp = client.get(WELL_KNOWN_AGENT_SKILLS_PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")

    body = resp.json()
    assert body["service"] == "cfactory"
    assert body["version"] == __version__
    assert body["mcp"] == {"transport": "http", "endpoint": "/mcp"}
    assert body["openapi"] == "/openapi.json"

    # Skills mirror the MCP tool catalog — the capabilities CFactory really has.
    assert [s["name"] for s in body["skills"]] == [t["name"] for t in MCP_TOOLS]
    assert all(s["description"] for s in body["skills"])


def test_manifest_advertises_reachable_surfaces(client):
    body = client.get(WELL_KNOWN_AGENT_SKILLS_PATH).json()
    assert client.get(body["openapi"]).status_code == 200
    # /mcp is POST-only JSON-RPC; a GET proves it is routed, not 404.
    assert client.get(body["mcp"]["endpoint"]).status_code != 404


def test_manifest_leaks_no_secrets_or_internal_hosts(client):
    # Public metadata only: relative paths, no upstream hostnames, no tokens.
    raw = client.get(WELL_KNOWN_AGENT_SKILLS_PATH).text
    assert "http://" not in raw
    assert "https://" not in raw
    for leak in ("token", "secret", "api_key", "password"):
        assert leak not in raw.lower()


def test_manifest_readable_without_a_key_when_keystore_is_enforced(client):
    set_keys({"k_ro": {"read"}})
    try:
        # Guarded surface rejects an unauthenticated caller ...
        assert client.get("/api/workitems").status_code == 401
        # ... while the discovery manifest stays open.
        assert client.get(WELL_KNOWN_AGENT_SKILLS_PATH).status_code == 200
    finally:
        reset_keystore()

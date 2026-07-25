"""Tests for the agent capability manifests (RFC-0019 §3.4).

Two properties matter: the shape is what a discovering agent expects, and it is
readable WITHOUT a credential — agents enumerate capabilities before they hold a
token. The second is asserted with the keystore ENFORCED (the rest of the suite
runs OPEN, where every path is reachable and would prove nothing).

Covers both documents: CFactory's own service manifest, and the fleet aggregate
that folds the three siblings in beside it.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from cfactory import __version__, routes_well_known
from cfactory.app import create_app, fleet_transport_dep, store_dep
from cfactory.auth import reset_keystore, set_keys
from cfactory.mcp import MCP_TOOLS
from cfactory.routes_well_known import (
    FLEET_AGENT_SKILLS_PATH,
    WELL_KNOWN_AGENT_SKILLS_PATH,
    reset_fleet_cache,
)
from cfactory.store import WorkItemStore


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def client(store):
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    return TestClient(app)


def _sibling_manifest(name: str) -> dict:
    """A minimal contract-shaped `kind: service` manifest, as a sibling serves it."""
    return {
        "schema_version": "1",
        "kind": "service",
        "service": {"name": name, "title": name.title(), "version": "9.9.9"},
        "openapi_url": "/openapi.json",
        "mcp": {"transport": "stdio"},
        "skills": [
            {
                "name": f"{name}-do-a-thing",
                "description": f"What {name} does.",
                "invocation": {"kind": "mcp_tool", "tool": "thing.do"},
            }
        ],
    }


def _fleet_transport(down: set[str] | None = None) -> httpx.MockTransport:
    """Serve each sibling's manifest, routed on port (default settings give each
    service its own localhost port). Names in ``down`` refuse the connection."""
    down = down or set()
    by_port = {3105: "pfactory", 3101: "aifactory", 3103: "tfactory"}

    def handler(request: httpx.Request) -> httpx.Response:
        name = by_port.get(request.url.port or 0, "")
        if not name or name in down:
            raise httpx.ConnectError("connection refused", request=request)
        assert request.url.path == WELL_KNOWN_AGENT_SKILLS_PATH
        return httpx.Response(200, json=_sibling_manifest(name))

    return httpx.MockTransport(handler)


@pytest.fixture
def fleet_client(store):
    """Client whose sibling fetches hit a MockTransport. The module-level
    last-good cache is cleared per test so cases cannot bleed into each other."""
    reset_fleet_cache()

    def _make(down: set[str] | None = None) -> TestClient:
        app = create_app()
        app.dependency_overrides[store_dep] = lambda: store
        app.dependency_overrides[fleet_transport_dep] = lambda: _fleet_transport(down)
        return TestClient(app)

    yield _make
    reset_fleet_cache()


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


def test_openapi_is_readable_without_a_key_when_keystore_is_enforced(client):
    """RFC-0019 §3.3: the OpenAPI document is enumerable before authenticating.

    FastAPI serves /openapi.json and the API-key middleware guards /api and
    /connect only, so this holds by construction — asserted here as a property
    so a later widening of the middleware's prefixes fails loudly instead of
    quietly closing the discovery surface (the manifest points agents at it).
    """
    set_keys({"k_ro": {"read"}})
    try:
        assert client.get("/api/workitems").status_code == 401
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")

        spec = resp.json()
        assert spec["openapi"].startswith("3.")
        assert spec["info"]["version"] == __version__
        # A real document, not an empty husk: the manifest paths are both in it.
        assert WELL_KNOWN_AGENT_SKILLS_PATH in spec["paths"]
        assert FLEET_AGENT_SKILLS_PATH in spec["paths"]
    finally:
        reset_keystore()


# ── Fleet aggregate ──────────────────────────────────────────────────────────


def test_fleet_manifest_shape(fleet_client):
    resp = fleet_client().get(FLEET_AGENT_SKILLS_PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    # Public, browser-reachable, and cached briefly so agents don't hammer it.
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert resp.headers["access-control-allow-origin"] == "*"

    body = resp.json()
    assert body["schema_version"] == "1"
    assert body["kind"] == "fleet"
    assert body["fleet"]["name"] == "factory"
    assert body["fleet"]["aggregator"] == "cfactory"
    assert body["generated_at"].endswith("Z")

    by_name = {s["service"]["name"]: s for s in body["services"]}
    assert set(by_name) == {"pfactory", "aifactory", "tfactory", "cfactory"}
    for name, entry in by_name.items():
        assert entry["manifest_url"].endswith(WELL_KNOWN_AGENT_SKILLS_PATH)
        assert "reachable" not in entry  # absent means reachable
        assert entry["fetched_at"].endswith("Z")
        # Every entry is a usable service manifest body.
        assert entry["openapi_url"]
        assert entry["mcp"]["transport"]
        assert entry["skills"] and all(
            s["name"] and s["description"] and s["invocation"]["kind"] for s in entry["skills"]
        )
        assert entry["service"]["version"]

    # The aggregate is a projection: sibling skills are folded in verbatim.
    assert by_name["pfactory"]["skills"] == _sibling_manifest("pfactory")["skills"]
    # CFactory's own entry mirrors its service manifest / MCP catalogue.
    assert by_name["cfactory"]["service"]["version"] == __version__
    assert [s["invocation"]["tool"] for s in by_name["cfactory"]["skills"]] == [
        t["name"] for t in MCP_TOOLS
    ]


def test_fleet_manifest_folds_in_nothing_beyond_the_contract():
    """A sibling manifest is copied field-by-field, so an origin that adds a
    field (a token, a tenant id, anything) cannot leak through this public
    endpoint — the aggregate carries the contract's fields and nothing else."""
    reset_fleet_cache()
    app = create_app()

    def handler(request: httpx.Request) -> httpx.Response:
        body = _sibling_manifest("pfactory") | {"upstream_token": "s3cret", "tenant": "acme"}
        return httpx.Response(200, json=body)

    app.dependency_overrides[fleet_transport_dep] = lambda: httpx.MockTransport(handler)
    raw = TestClient(app).get(FLEET_AGENT_SKILLS_PATH).text
    reset_fleet_cache()
    assert "s3cret" not in raw
    assert "upstream_token" not in raw
    assert "acme" not in raw


def test_fleet_manifest_survives_a_sibling_being_down(fleet_client):
    body = fleet_client(down={"aifactory"}).get(FLEET_AGENT_SKILLS_PATH).json()

    by_name = {s["service"]["name"]: s for s in body["services"]}
    assert set(by_name) == {"pfactory", "aifactory", "tfactory", "cfactory"}
    # The dead sibling is announced as unavailable, not dropped and not fatal ...
    assert by_name["aifactory"]["reachable"] is False
    assert "skills" not in by_name["aifactory"]
    # ... and the rest of the fleet is still fully usable.
    assert by_name["pfactory"]["skills"]
    assert by_name["tfactory"]["skills"]
    assert by_name["cfactory"]["skills"]


def test_fleet_manifest_serves_the_last_good_copy_when_a_sibling_dies(fleet_client, monkeypatch):
    up = fleet_client().get(FLEET_AGENT_SKILLS_PATH).json()
    fresh = {s["service"]["name"]: s for s in up["services"]}["tfactory"]

    # Expire the cache so the next request really re-fetches — otherwise this
    # would only prove the 60s TTL is being honoured.
    monkeypatch.setattr(routes_well_known, "_FLEET_CACHE_TTL_SECONDS", 0.0)
    # Same process, tfactory now refusing connections, last-good copy retained.
    body = fleet_client(down={"tfactory"}).get(FLEET_AGENT_SKILLS_PATH).json()
    stale = {s["service"]["name"]: s for s in body["services"]}["tfactory"]
    assert stale["skills"] == fresh["skills"]
    assert stale["fetched_at"] == fresh["fetched_at"]  # when the fetch last succeeded
    assert stale["reachable"] is False


def test_fleet_manifest_readable_without_a_key_when_keystore_is_enforced(fleet_client):
    client = fleet_client()
    set_keys({"k_ro": {"read"}})
    try:
        assert client.get("/api/workitems").status_code == 401
        assert client.get(FLEET_AGENT_SKILLS_PATH).status_code == 200
    finally:
        reset_keystore()

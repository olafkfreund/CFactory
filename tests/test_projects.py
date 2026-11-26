"""The portal's project rules, exposed through tenant-bound read surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cfactory import api_deps, git_config_ops, mcp, projects, routes_git_config
from cfactory.app import create_app
from cfactory.auth import READ
from cfactory.cards import CardStore
from cfactory.config import Settings
from fastapi.testclient import TestClient

FIXTURE = json.loads((Path(__file__).parent / "data/projects.json").read_text())


def test_project_rules() -> None:
    derived = projects.derive_projects(FIXTURE["repositories"])
    assert derived == FIXTURE["expected_projects"]
    all_projects = derived + FIXTURE["extra_projects"]
    assert projects.project_slugs(all_projects) == FIXTURE["expected_slugs"]
    for case in FIXTURE["references"]:
        found = projects.find_project(all_projects, case["ref"])
        assert (found["id"] if found else None) == case["id"]
    assert projects.find_project(derived, "demo") is None
    assert projects.slugify("Café / Widgets", "fallback") == "cafe-widgets"
    assert projects.slugify("---", "fallback") == "fallback"


def test_rest_and_mcp_are_read_scoped_and_tenant_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    settings = Settings(multi_tenant=True)
    monkeypatch.setattr(api_deps, "get_settings", lambda: settings)
    monkeypatch.setattr(api_deps, "get_cards_store", lambda: cards)
    monkeypatch.setattr(routes_git_config, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp, "_authenticate", lambda _request: (frozenset({READ}), False))
    monkeypatch.setattr(mcp, "get_keystore", lambda: None)
    monkeypatch.setattr(mcp, "identity_dep", lambda **_kwargs: "test")

    seen: list[str] = []

    def repositories(store: CardStore, _connection_id: int | None = None) -> dict[str, Any]:
        seen.append(store.tenant)
        return {"repositories": FIXTURE["repositories"] if store.tenant == "acme" else []}

    monkeypatch.setattr(git_config_ops, "list_git_repositories", repositories)
    client = TestClient(create_app())
    headers = {"X-Tenant-Id": "acme"}
    url = "/api/tenants/acme/projects"
    listed = client.get(url, headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["repository_ids"] == [1, 2]
    assert listed.json()[0]["slug"] == "acme-other"
    assert client.get(f"{url}/resolve", params={"ref": "demo"}, headers=headers).json() == {
        "error": "ambiguous"
    }
    assert client.get(f"{url}/resolve", params={"ref": "missing"}, headers=headers).json() == {
        "error": "unknown"
    }
    assert (
        client.get(f"{url}/resolve", params={"ref": "acme-other"}, headers=headers).json()[
            "project"
        ]["id"]
        == listed.json()[0]["id"]
    )
    assert client.get(url, headers={"X-Tenant-Id": "other"}).status_code == 403
    assert client.get("/api/tenants/other/projects", headers={"X-Tenant-Id": "other"}).json() == []
    assert seen == ["acme", "acme", "acme", "acme", "other"]

    assert mcp.TOOL_SCOPES["cfactory_list_projects"] == READ
    assert mcp.TOOL_SCOPES["cfactory_resolve_project"] == READ
    for name, args in (
        ("cfactory_list_projects", {}),
        ("cfactory_resolve_project", {"ref": "acme-other"}),
    ):
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        assert response.status_code == 200
        assert "error" not in response.json()
    other = client.post(
        "/mcp",
        headers={"X-Tenant-Id": "other"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_list_projects", "arguments": {}},
        },
    )
    assert json.loads(other.json()["result"]["content"][0]["text"]) == []
    assert seen[-3:] == ["acme", "acme", "other"]

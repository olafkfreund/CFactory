"""The portal's project rules, exposed through tenant-bound read surfaces."""

from __future__ import annotations

import json
from datetime import datetime
from math import trunc
from pathlib import Path

import pytest
from cfactory import api_deps, auth, mcp, projects, routes_git_config
from cfactory.app import create_app
from cfactory.audit import AuditStore
from cfactory.auth import READ
from cfactory.cards import CardStore
from cfactory.config import Settings
from cfactory.git_connections import GitConnectionCreate, GitRepositoryCreate
from fastapi.testclient import TestClient

FIXTURE = json.loads((Path(__file__).parent / "data/projects.json").read_text())


def test_project_rules() -> None:
    derived = projects.derive_projects(FIXTURE["repositories"])
    assert derived == [
        {key: value for key, value in project.items() if key != "server_canonical"}
        for project in FIXTURE["expected_projects"]
    ]
    all_projects = derived + FIXTURE["extra_projects"]
    assert projects.project_slugs(all_projects) == FIXTURE["expected_slugs"]
    for case in FIXTURE["references"]:
        found = projects.find_project(all_projects, case["ref"])
        assert (found["id"] if found else None) == case["id"]
    assert projects.find_project(derived, "demo") is None
    assert projects.slugify("Café / Widgets", "fallback") == "cafe-widgets"
    assert projects.slugify("---", "fallback") == "fallback"
    assert projects._created(FIXTURE["extra_projects"][1]) == projects._created(
        FIXTURE["extra_projects"][2]
    )
    assert projects._created(FIXTURE["extra_projects"][3]) == projects._created(
        FIXTURE["extra_projects"][4]
    )
    missing_slug = {"id": "x", "name": "X", "repo_paths": [], "created_at": "", "slug": None}
    assert projects.matching_projects([missing_slug], "") == []
    assert projects.find_project([missing_slug], "missing") is None
    assert (
        projects.matching_projects(
            [{"id": "x", "name": "X", "repo_paths": [], "created_at": ""}], ""
        )
        == []
    )


def test_date_only_is_utc_but_naive_datetime_is_local() -> None:
    # JS: new Date("YYYY-MM-DD") is UTC; a datetime without an offset is local time.
    # Compared against this process's own local time, so other tests' TZ state can't break it.
    day = projects._created({"created_at": "2026-05-01"})
    assert day == projects._created({"created_at": "2026-05-01T00:00:00Z"})
    local = trunc(datetime(2026, 5, 1).timestamp() * 1000)  # noqa: DTZ001 - naive on purpose
    assert projects._created({"created_at": "2026-05-01T00:00:00"}) == local


def test_rest_and_mcp_are_read_scoped_and_tenant_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    acme = cards.scoped("acme")
    other_store = cards.scoped("other")
    acme_connection = acme.create_connection(GitConnectionCreate())
    other_connection = other_store.create_connection(GitConnectionCreate())
    for path in ("acme/widgets", "acme/other"):
        acme.create_repository(
            acme_connection.id,
            GitRepositoryCreate(project=path, aifactory_project_id="acme-project"),
        )
    other_store.create_repository(other_connection.id, GitRepositoryCreate(project="other/private"))
    settings = Settings(multi_tenant=True)
    keystore = auth.KeyStore({"reader-key": {READ}, "writer-key": {"write"}})
    monkeypatch.setattr(api_deps, "get_settings", lambda: settings)
    monkeypatch.setattr(api_deps, "get_cards_store", lambda: cards)
    monkeypatch.setattr(routes_git_config, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp, "get_keystore", lambda: keystore)
    monkeypatch.setattr(
        mcp, "cards_store_dep", lambda tenant=None: cards.scoped(tenant or "default")
    )
    monkeypatch.setattr(
        mcp,
        "get_audit_store",
        lambda: AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="test"),  # noqa: S106 - test-only HMAC key
    )
    app = create_app()
    app.dependency_overrides[auth.keystore_dep] = lambda: keystore
    client = TestClient(app)
    headers = {"X-Tenant-Id": "acme", "Authorization": "Bearer reader-key"}
    url = "/api/tenants/acme/projects"
    listed = client.get(url, headers=headers)
    assert listed.status_code == 200
    assert listed.json() == [
        {
            "id": "acme-project",
            "name": "widgets",
            "slug": "widgets",
            "created_at": listed.json()[0]["created_at"],
            "repository_ids": [1, 2],
            "repo_paths": ["acme/other", "acme/widgets"],
        }
    ]
    assert client.get(f"{url}/resolve", params={"ref": "missing"}, headers=headers).json() == {
        "error": "unknown"
    }
    assert (
        client.get(f"{url}/resolve", params={"ref": "widgets"}, headers=headers).json()["project"][
            "id"
        ]
        == "acme-project"
    )
    assert client.get(url, headers={**headers, "X-Tenant-Id": "other"}).status_code == 403
    other_headers = {**headers, "X-Tenant-Id": "other"}
    other_projects = client.get("/api/tenants/other/projects", headers=other_headers).json()
    assert len(other_projects) == 1
    assert other_projects[0]["repo_paths"] == ["other/private"]
    assert other_projects[0]["id"] != "acme-project"
    assert (
        client.get(url, headers={**headers, "Authorization": "Bearer writer-key"}).status_code
        == 403
    )

    assert mcp.TOOL_SCOPES["cfactory_list_projects"] == READ
    assert mcp.TOOL_SCOPES["cfactory_resolve_project"] == READ
    for name, args, expected in (
        ("cfactory_list_projects", {}, listed.json()),
        ("cfactory_resolve_project", {"ref": "widgets"}, {"project": listed.json()[0]}),
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
        result = response.json()["result"]
        assert result.get("isError") is not True
        assert json.loads(result["content"][0]["text"]) == expected
    other = client.post(
        "/mcp",
        headers=other_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_list_projects", "arguments": {}},
        },
    )
    assert json.loads(other.json()["result"]["content"][0]["text"]) == other_projects
    denied = client.post(
        "/mcp",
        headers={**headers, "Authorization": "Bearer writer-key"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_list_projects", "arguments": {}},
        },
    )
    assert denied.status_code == 403
    assert "read" in denied.json()["detail"]

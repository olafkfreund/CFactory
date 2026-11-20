"""Step 4 of #561: the switch, the proposal read and card creation."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from cfactory import auth, config, mcp
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from cfactory.git_connections import GitConnectionCreate, GitRepositoryCreate
from fastapi.testclient import TestClient

_KEY = "k" * 32
_READ_KEY = "r" * 32
_SHA = "a" * 40


class FakeGitHub:
    def __init__(self) -> None:
        self.branch = "main"
        self.empty = False
        self.fail = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(500, json={})
        path = request.url.raw_path.decode()
        if "/commits/" in path:
            if self.empty:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            return httpx.Response(200, json={"sha": _SHA})
        return httpx.Response(200, json={"default_branch": self.branch})


@pytest.fixture
def gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def store(tmp_path) -> CardStore:
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path) -> AuditStore:
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="inv-ctx-test")  # noqa: S106


@pytest.fixture
def repo_id(store) -> int:
    conn = store.create_connection(GitConnectionCreate(provider="github"))
    return store.create_repository(
        conn.id, GitRepositoryCreate(project="acme/widgets", aifactory_project_id="proj-1")
    ).id


def _app(  # noqa: PLR0913 — a test helper
    monkeypatch, store, audit, gh, *, switch="on", multi_tenant=False
):
    monkeypatch.setenv("CFACTORY_INVESTIGATIONS", switch)
    monkeypatch.setenv("CFACTORY_MULTI_TENANT", "true" if multi_tenant else "false")
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    config.reset_settings()
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: store)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: store
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = lambda: httpx.MockTransport(gh)
    return app


@pytest.fixture
def client(monkeypatch, store, audit, gh):
    auth.set_keys({_KEY: {"read", "write"}, _READ_KEY: {"read"}})
    yield TestClient(
        _app(monkeypatch, store, audit, gh), headers={"Authorization": f"Bearer {_KEY}"}
    )
    auth.reset_keystore()
    config.reset_settings()


def _entries(audit, kind):
    return [e for e in reversed(audit.list(limit=200)) if e.kind == kind]  # oldest first


def test_router_absent_when_off(monkeypatch, store, audit, gh):
    c = TestClient(_app(monkeypatch, store, audit, gh, switch="off"))
    assert c.get("/api/investigations/context").status_code == 404
    assert c.post("/api/investigations/cards", json={}).status_code == 404
    config.reset_settings()


def test_context_uses_default_and_names_it(client, repo_id):
    body = client.get("/api/investigations/context").json()
    assert body == {
        "repository_id": repo_id,
        "repo": "acme/widgets",
        "default_branch": "main",
        "sha": _SHA,
        "test_command": None,
    }


def test_context_slash_in_branch_is_quoted(client, gh, repo_id):
    gh.branch = "feature/x y"
    assert client.get(f"/api/investigations/context?repository_id={repo_id}").status_code == 200
    assert "/commits/feature%2Fx%20y" in gh.requests[-1].url.raw_path.decode()


def test_context_returns_the_command(client, repo_id):
    client.patch(
        f"/api/tenants/default/git-repositories/{repo_id}",
        json={"investigate_test_command": "pytest -q"},
    )
    assert client.get("/api/investigations/context").json()["test_command"] == "pytest -q"


def _reason(resp):
    return resp.json()["detail"]["reason"]


@pytest.mark.usefixtures("repo_id")
def test_context_refusals(client, store, gh):
    resp = client.get("/api/investigations/context?repository_id=9999")
    assert (resp.status_code, _reason(resp)) == (404, "no_repository")
    gh.empty = True
    resp = client.get("/api/investigations/context")
    assert (resp.status_code, _reason(resp)) == (409, "empty_repository")
    gh.empty, gh.fail = False, True
    resp = client.get("/api/investigations/context")
    assert (resp.status_code, _reason(resp)) == (502, "provider_unavailable")
    gh.fail = False
    conn = store.create_connection(
        GitConnectionCreate(provider="gitlab", base_url="https://gl.example.com")
    )
    gl = store.create_repository(
        conn.id, GitRepositoryCreate(project="a/b", aifactory_project_id="p")
    ).id
    resp = client.get(f"/api/investigations/context?repository_id={gl}")
    assert (resp.status_code, _reason(resp)) == (409, "unsupported_provider")
    ghc = store.create_connection(
        GitConnectionCreate(provider="github", base_url="https://ghe.example.com")
    )
    bare = store.create_repository(ghc.id, GitRepositoryCreate(project="a/c")).id
    resp = client.get(f"/api/investigations/context?repository_id={bare}")
    assert (resp.status_code, _reason(resp)) == (409, "no_aifactory_project")


def test_no_default_repository(client):
    resp = client.get("/api/investigations/context")
    assert (resp.status_code, _reason(resp)) == (404, "no_repository")


@pytest.mark.usefixtures("repo_id")
def test_create_requires_an_explicit_repository(client):
    assert client.post("/api/investigations/cards", json={"request": "why?"}).status_code == 422


def test_create_card_title_description_and_audit(client, store, audit, repo_id):
    request = "x" * 300
    resp = client.post(
        "/api/investigations/cards", json={"request": request, "repository_id": repo_id}
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert len(card["title"]) == 214 and card["title"].endswith("…")
    assert card["title"].startswith("Investigate: ")
    assert card["description"] == request
    assert card["tier"] is None and card["issue_ref"] is None and card["status"] == "backlog"
    assert card["repository_id"] == repo_id and "kind" not in card
    stored = store.get(card["card_key"])
    assert stored.is_investigation
    binding = stored.stage_runs["investigate"]["detail"]["binding"]
    assert binding == {
        "repository_id": repo_id,
        "repo": "acme/widgets",
        "aifactory_project_id": "proj-1",
    }
    (entry,) = _entries(audit, "create_investigation_card")
    digest = hashlib.sha256(request.encode()).hexdigest()
    assert entry.endpoint == f"card/{card['card_key']}:request={digest}"
    assert entry.correlation_key == card["card_key"]
    assert request not in json.dumps([e.model_dump(mode="json") for e in audit.list(limit=50)])


@pytest.mark.parametrize(
    "secret",
    [
        "see ghp_abcdef",
        "gho_abc",
        "github_pat_11ABC",
        "key sk-live123",
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN PRIVATE KEY-----",
        "password=hunter2",
        "TOKEN = abc",
    ],
)
def test_secret_requests_are_refused(client, store, repo_id, secret):
    resp = client.post(
        "/api/investigations/cards", json={"request": secret, "repository_id": repo_id}
    )
    assert (resp.status_code, _reason(resp)) == (422, "request_contains_secret")
    assert store.list() == []


def test_create_refuses_a_missing_repository_and_an_empty_one(client, gh, repo_id):
    resp = client.post("/api/investigations/cards", json={"request": "ok", "repository_id": 999})
    assert (resp.status_code, _reason(resp)) == (404, "no_repository")
    gh.empty = True
    resp = client.post(
        "/api/investigations/cards", json={"request": "ok", "repository_id": repo_id}
    )
    assert (resp.status_code, _reason(resp)) == (409, "empty_repository")


def test_write_needs_write_scope(monkeypatch, store, audit, gh, repo_id):
    auth.set_keys({_READ_KEY: {"read"}})
    try:
        c = TestClient(
            _app(monkeypatch, store, audit, gh), headers={"Authorization": f"Bearer {_READ_KEY}"}
        )
        resp = c.post("/api/investigations/cards", json={"request": "ok", "repository_id": repo_id})
        assert resp.status_code == 403
        assert c.get("/api/investigations/context").status_code == 200
    finally:
        auth.reset_keystore()
        config.reset_settings()


def test_dev_open_gives_keys_required(monkeypatch, store, audit, gh, repo_id):
    auth.reset_keystore()  # tests/conftest.py sets CFACTORY_API_DEV_OPEN=true
    c = TestClient(_app(monkeypatch, store, audit, gh))
    resp = c.post("/api/investigations/cards", json={"request": "ok", "repository_id": repo_id})
    assert (resp.status_code, _reason(resp)) == (403, "keys_required")
    assert c.get("/api/investigations/context").status_code == 200  # reads stay open
    config.reset_settings()


def test_multi_tenant_is_refused(monkeypatch, store, audit, gh, repo_id):
    auth.set_keys({_KEY: {"read", "write"}})
    try:
        c = TestClient(
            _app(monkeypatch, store, audit, gh, multi_tenant=True),
            headers={"Authorization": f"Bearer {_KEY}", "X-Tenant-Id": "default"},
        )
        for resp in (
            c.get("/api/investigations/context"),
            c.post("/api/investigations/cards", json={"request": "ok", "repository_id": repo_id}),
        ):
            assert (resp.status_code, _reason(resp)) == (409, "multi_tenant_unsupported")
    finally:
        auth.reset_keystore()
        config.reset_settings()


def test_drain_refuses_create_but_not_reads(monkeypatch, store, audit, gh, repo_id):
    auth.set_keys({_KEY: {"read", "write"}})
    try:
        c = TestClient(
            _app(monkeypatch, store, audit, gh, switch="drain"),
            headers={"Authorization": f"Bearer {_KEY}"},
        )
        resp = c.post("/api/investigations/cards", json={"request": "ok", "repository_id": repo_id})
        assert (resp.status_code, _reason(resp)) == (503, "investigations_draining")
        assert c.get("/api/investigations/context").status_code == 200
    finally:
        auth.reset_keystore()
        config.reset_settings()


def _command_entry(repo_id, old, new):
    h = lambda v: hashlib.sha256(v.encode()).hexdigest()  # noqa: E731
    return f"repo/{repo_id}:old={h(old)};new={h(new)}"


def test_command_set_is_audited_with_hashes_over_rest(client, audit, repo_id):
    url = f"/api/tenants/default/git-repositories/{repo_id}"
    assert client.patch(url, json={"investigate_test_command": "pytest -q"}).status_code == 200
    assert client.patch(url, json={"investigate_test_command": ""}).status_code == 200
    assert client.patch(url, json={"project": "acme/widgets"}).status_code == 200  # not audited
    entries = _entries(audit, "set_investigate_test_command")
    assert [e.endpoint for e in entries] == [
        _command_entry(repo_id, "", "pytest -q"),
        _command_entry(repo_id, "pytest -q", ""),
    ]
    assert "pytest" not in json.dumps([e.model_dump(mode="json") for e in audit.list(limit=50)])


def _tool(client, name, arguments):
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def test_command_round_trip_over_mcp(client, audit, repo_id):
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    schema = next(
        t for t in tools["result"]["tools"] if t["name"] == "cfactory_update_git_repository"
    )["inputSchema"]
    assert "investigate_test_command" in schema["properties"]
    result = _tool(
        client,
        "cfactory_update_git_repository",
        {"repository_id": repo_id, "investigate_test_command": "make test"},
    )
    assert json.loads(result["content"][0]["text"])["investigate_test_command"] == "make test"
    (entry,) = _entries(audit, "set_investigate_test_command")
    assert entry.endpoint == _command_entry(repo_id, "", "make test")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"status": "identical"}, True),
        (200, {"status": "behind"}, True),
        (200, {"status": "ahead"}, False),
        (200, {"status": "diverged"}, False),
        (404, {}, False),
        (422, {}, False),
    ],
)
def test_is_reachable_from_default(status, body, expected):
    import asyncio  # noqa: PLC0415

    from cfactory.git_providers import HttpGitHubProvider  # noqa: PLC0415

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(status, json=body)

    provider = HttpGitHubProvider(
        "a/b", "t", "https://api.github.com", httpx.MockTransport(handler)
    )
    assert asyncio.run(provider.is_reachable_from_default("a/b", "f/x", _SHA)) is expected
    assert seen == [f"/repos/a/b/compare/f%2Fx...{_SHA}"]


def test_is_reachable_other_errors_raise():
    import asyncio  # noqa: PLC0415

    from cfactory.git_providers import HttpGitHubProvider  # noqa: PLC0415

    provider = HttpGitHubProvider(
        "a/b", "t", "https://api.github.com", httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(provider.is_reachable_from_default("a/b", "main", _SHA))

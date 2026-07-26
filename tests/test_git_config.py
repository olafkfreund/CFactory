"""Tenant-scoped git configuration (RFC-0020 §3.3, #363).

The properties this phase has to hold, one test each:

* **CRUD round-trips** over REST and over MCP, with the derived status right at
  every step (unconfigured -> credential_missing -> configured -> verified);
* **tenant isolation** — tenant A can neither read nor write tenant B's
  configuration, and cannot get at it by naming B in the URL. This is the guard
  the mutation check weakens;
* **scope** — a read-scoped key may read the configuration and may not change it,
  over either transport;
* **verify makes exactly ONE provider call** and records what it found;
* **the seed materialises from the environment once** and is never re-applied
  over an edited configuration. This is the other guard the mutation check
  weakens: a seed that re-ran would silently undo every operator edit on each
  restart, which is the single worst failure available to this feature;
* **a GitLab configuration drives the GitLab provider end to end**, so the panel
  is not a GitHub feature with three options in a dropdown.
"""

from __future__ import annotations

import json

import httpx
import pytest
from cfactory import (
    api_deps,
    auth,
    card_ops,
    config,
    git_config,
    git_config_ops,
    github_sync,
    issue_import,
    mcp,
    routes_git_config,
)
from cfactory import cards as cards_module
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.config import Settings
from cfactory.git_config import (
    CONFIGURED,
    CREDENTIAL_MISSING,
    UNCONFIGURED,
    VERIFIED,
    GitConfigError,
    GitConfigUpdate,
)
from fastapi.testclient import TestClient
from runners.github.providers.gitlab_provider import GitLabProvider

# Not credentials: fake values so the resolved config has a token and the API
# keystore is configured inside these tests.
_TEST_TOKEN = "test-git-token-not-a-credential"  # noqa: S105 — a fake, not a secret
_WRITER = "test-writer-key-not-a-credential"  # noqa: S105 — a fake, not a secret
_READER = "test-reader-key-not-a-credential"  # noqa: S105 — a fake, not a secret
_PROJECT = "acme/widgets"
_TENANT = "default"

_HTTP_FORBIDDEN = 403


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="git-config-test-hmac")  # noqa: S106 — a test fixture, not a secret


@pytest.fixture
def ctx(audit):
    return AuditContext(audit, "tester")


class FakeHost:
    """A stand-in git host that records every request it is asked to serve."""

    def __init__(self, *, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self.body = {"full_name": _PROJECT, "default_branch": "main"} if body is None else body
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"message": "boom"})
        return httpx.Response(200, json=self.body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def settings():
    """A deployment with a credential and NOTHING else configured, so what the
    tests exercise is the stored configuration rather than the env fallback."""
    return Settings(git_provider_token=_TEST_TOKEN, github_api_url="https://gh.test")


@pytest.fixture(autouse=True)
def _settings(monkeypatch, settings):
    for module in (cards_module, git_config, git_config_ops, github_sync, issue_import):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def client(cards, audit, host, monkeypatch, _settings):
    """One TestClient serving BOTH surfaces over one store, so REST and MCP can be
    compared directly (RFC-0019 §3.3)."""
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", host.transport)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.set_keys({_WRITER: {"read", "write"}, _READER: {"read"}})

    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = host.transport
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.reset_keystore()


def _url(tenant: str = _TENANT) -> str:
    return f"/api/tenants/{tenant}/git-config"


def _call_tool(client, name: str, arguments: dict | None = None, key: str = _WRITER):
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    return resp


def _tool_payload(client, name: str, arguments: dict | None = None) -> dict:
    resp = _call_tool(client, name, arguments)
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


# ── CRUD round trip ──────────────────────────────────────────────────────────


def test_an_unconfigured_tenant_reads_as_unconfigured_not_as_a_404(client):
    """There is always somewhere for the panel to render: no project named is a
    state, not a missing resource."""
    body = client.get(_url()).json()

    assert body["status"] == UNCONFIGURED
    assert body["project"] is None
    assert body["provider"] == "github"
    assert body["source"] == "env"


def test_put_then_get_round_trips_every_field(client):
    put = client.put(
        _url(),
        json={
            "provider": "gitlab",
            "base_url": "https://gitlab.example.com",
            "project": "acme/group/widgets",
            "intake_project": "acme/group/legacy",
            "aifactory_project_id": "5d78d4b9-35f9-4445-92c1-78f3ff60a494",
            "default_labels": ["board"],
        },
    )
    assert put.status_code == 200, put.text

    body = client.get(_url()).json()
    assert body["provider"] == "gitlab"
    assert body["base_url"] == "https://gitlab.example.com"
    assert body["project"] == "acme/group/widgets"
    assert body["intake_project"] == "acme/group/legacy"
    assert body["aifactory_project_id"] == "5d78d4b9-35f9-4445-92c1-78f3ff60a494"
    assert body["default_labels"] == ["board"]
    assert body["source"] == "stored"
    # A credential is configured in these tests, so a named project is reachable
    # in principle — but nothing has proved it yet.
    assert body["status"] == CONFIGURED


def test_put_is_a_replacement_so_an_omitted_field_is_cleared(client):
    client.put(_url(), json={"provider": "github", "project": _PROJECT, "intake_project": "a/b"})

    client.put(_url(), json={"provider": "github", "project": _PROJECT})

    assert client.get(_url()).json()["intake_project"] is None


def test_no_credential_is_ever_stored_or_returned(client, cards):
    """The copilot precedent: provider + project persist, the key never does."""
    client.put(_url(), json={"provider": "github", "project": _PROJECT, "base_url": "https://x.y"})

    body = client.get(_url()).json()

    assert not any("token" in key or "secret" in key for key in body)
    row = cards.git_config_row()
    assert not any("token" in c.name for c in row.__table__.columns)


def test_the_mcp_twin_reads_and_writes_the_same_configuration(client):
    """RFC-0019 §3.3 parity is a property of one implementation, not a
    coincidence between two."""
    _tool_payload(client, "cfactory_set_git_config", {"provider": "github", "project": _PROJECT})

    assert client.get(_url()).json()["project"] == _PROJECT
    assert _tool_payload(client, "cfactory_get_git_config")["project"] == _PROJECT


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"provider": "bitbucket"}, "unknown provider"),
        ({"provider": "github", "project": "../../etc"}, "project path"),
        ({"provider": "azure_devops", "project": "acme/widgets"}, "organization/project/repo"),
        ({"provider": "github", "base_url": "file:///etc"}, "http"),
        ({"provider": "github", "default_labels": ["factory:low"]}, "intake trigger"),
    ],
)
def test_a_configuration_the_provider_could_not_use_is_refused(client, payload, expected):
    """Refused at the boundary, not at the next card write: a config that saves
    cleanly and then breaks every sync is the worst of both."""
    resp = client.put(_url(), json=payload)

    assert resp.status_code == 400
    assert expected in resp.json()["detail"]


def test_the_intake_trigger_label_is_refused_over_mcp_too(client):
    payload = _tool_payload(
        client, "cfactory_set_git_config", {"project": _PROJECT, "default_labels": ["factory:hard"]}
    )

    assert "intake trigger" in payload["error"]


def test_a_project_path_cannot_smuggle_a_traversal(client):
    """The stored project is interpolated into a request PATH by the provider, so
    it is a trust boundary — validated where it enters, once."""
    with pytest.raises(GitConfigError):
        git_config.validate_project("acme/../../admin", "github")


# ── derived status ───────────────────────────────────────────────────────────


def test_status_transitions_through_every_state(cards, client, host, monkeypatch, settings):
    # 1. Nothing configured.
    assert client.get(_url()).json()["status"] == UNCONFIGURED

    # 2. A project, but the deployment has no usable credential. This is exactly
    #    what phases 3-4 will fix; until then it is reported, not hidden.
    monkeypatch.setattr(settings, "git_provider_token", None)
    client.put(_url(), json={"provider": "github", "project": _PROJECT})
    assert client.get(_url()).json()["status"] == CREDENTIAL_MISSING

    # 3. Credential restored: reachable in principle, never proved.
    monkeypatch.setattr(settings, "git_provider_token", _TEST_TOKEN)
    assert client.get(_url()).json()["status"] == CONFIGURED

    # 4. Proved.
    assert client.post(f"{_url()}:verify").json()["ok"] is True
    assert client.get(_url()).json()["status"] == VERIFIED

    # 5. And an edit takes the proof away with it — it proved another config.
    client.put(_url(), json={"provider": "github", "project": "acme/other"})
    assert client.get(_url()).json()["status"] == CONFIGURED


def test_a_withdrawn_credential_outranks_an_earlier_verification(client, monkeypatch, settings):
    """Reporting a stale green for a project that can no longer be reached would
    be the most misleading answer available."""
    client.put(_url(), json={"provider": "github", "project": _PROJECT})
    client.post(f"{_url()}:verify")
    assert client.get(_url()).json()["status"] == VERIFIED

    monkeypatch.setattr(settings, "git_provider_token", None)

    assert client.get(_url()).json()["status"] == CREDENTIAL_MISSING


# ── verify ───────────────────────────────────────────────────────────────────


def test_verify_makes_exactly_one_provider_call_and_records_it(client, host):
    client.put(_url(), json={"provider": "github", "project": _PROJECT})

    body = client.post(f"{_url()}:verify").json()

    assert len(host.requests) == 1, "verify must be ONE cheap read, not a probe sweep"
    assert host.requests[0].url.path == f"/repos/{_PROJECT}"
    assert body["ok"] is True
    assert body["repository"] == _PROJECT
    assert body["config"]["status"] == VERIFIED


def test_a_failed_verify_records_the_reason_and_does_not_claim_verified(client, host, cards):
    client.put(_url(), json={"provider": "github", "project": _PROJECT})
    host.status_code = 404

    body = client.post(f"{_url()}:verify").json()

    assert body["ok"] is False
    assert "404" in body["reason"]
    assert body["config"]["status"] == CONFIGURED
    assert "404" in cards.git_config_row().verify_error


def test_verify_without_a_credential_says_so_without_calling_anything(
    client, host, monkeypatch, settings
):
    client.put(_url(), json={"provider": "github", "project": _PROJECT})
    monkeypatch.setattr(settings, "git_provider_token", None)

    body = client.post(f"{_url()}:verify").json()

    assert body["ok"] is False
    assert body["status"] == CREDENTIAL_MISSING
    assert host.requests == []


def test_verify_over_mcp_is_the_same_single_call(client, host):
    _tool_payload(client, "cfactory_set_git_config", {"project": _PROJECT})

    payload = _tool_payload(client, "cfactory_verify_git_config")

    assert payload["ok"] is True
    assert len(host.requests) == 1


# ── audit chain ──────────────────────────────────────────────────────────────


def test_every_mutation_is_audit_chained(client, audit):
    client.put(_url(), json={"provider": "github", "project": _PROJECT})
    client.post(f"{_url()}:verify")

    kinds = [(e.kind, e.ok) for e in audit.list()]
    assert ("set_git_config", True) in kinds
    assert ("verify_git_config", True) in kinds
    assert audit.verify() == [], "the tamper-evident chain must still be intact"


# ── scope ────────────────────────────────────────────────────────────────────


def test_a_read_scoped_key_can_read_and_cannot_write(client):
    reader = {"Authorization": f"Bearer {_READER}"}

    assert client.get(_url(), headers=reader).status_code == 200
    assert client.put(_url(), json={"provider": "github"}, headers=reader).status_code == 403
    assert client.post(f"{_url()}:verify", headers=reader).status_code == 403


def test_a_read_scoped_key_cannot_write_over_mcp_either(client):
    assert _call_tool(client, "cfactory_get_git_config", key=_READER).status_code == 200
    assert _call_tool(client, "cfactory_set_git_config", {}, key=_READER).status_code == 403
    assert _call_tool(client, "cfactory_verify_git_config", key=_READER).status_code == 403


# ── tenant isolation (the first mutation-check guard) ────────────────────────


@pytest.fixture
def multi_tenant_client(cards, audit, host, monkeypatch, settings):
    """The same app with CFACTORY_MULTI_TENANT on, so the tenant comes from the
    X-Tenant-Id header oauth2-proxy injects from the Keycloak claim.

    The REAL ``cards_store_dep`` runs here (only the underlying store is a temp
    one), because the thing under test IS the scoping the dependency performs —
    overriding it would hand every tenant the same view and prove nothing.
    """
    monkeypatch.setattr(settings, "multi_tenant", True)
    monkeypatch.setattr(api_deps, "get_settings", lambda: settings)
    monkeypatch.setattr(api_deps, "get_cards_store", lambda: cards)
    monkeypatch.setattr(routes_git_config, "get_settings", lambda: settings)
    auth.set_keys({_WRITER: {"read", "write"}})

    app = create_app()
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = host.transport
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.reset_keystore()


def _as(client, tenant: str, method: str, path: str, **kwargs):
    return getattr(client, method)(path, headers={"X-Tenant-Id": tenant}, **kwargs)


def test_a_tenant_cannot_read_or_write_another_tenants_config(multi_tenant_client):
    """THE isolation contract. The tenant in the URL is an assertion the caller
    makes about itself, and it is checked against the resolved identity."""
    client = multi_tenant_client
    _as(client, "acme", "put", _url("acme"), json={"provider": "github", "project": _PROJECT})

    assert _as(client, "globex", "get", _url("acme")).status_code == _HTTP_FORBIDDEN
    assert (
        _as(client, "globex", "put", _url("acme"), json={"provider": "gitlab"}).status_code
        == _HTTP_FORBIDDEN
    )
    assert _as(client, "globex", "post", f"{_url('acme')}:verify").status_code == _HTTP_FORBIDDEN


def test_each_tenant_sees_only_its_own_configuration(multi_tenant_client):
    client = multi_tenant_client
    _as(client, "acme", "put", _url("acme"), json={"provider": "github", "project": "acme/widgets"})
    _as(
        client,
        "globex",
        "put",
        _url("globex"),
        json={"provider": "gitlab", "project": "globex/gadgets"},
    )

    assert _as(client, "acme", "get", _url("acme")).json()["project"] == "acme/widgets"
    assert _as(client, "globex", "get", _url("globex")).json()["project"] == "globex/gadgets"


def test_a_write_lands_in_the_writers_partition_not_the_default_one(multi_tenant_client, cards):
    client = multi_tenant_client
    _as(client, "acme", "put", _url("acme"), json={"provider": "github", "project": _PROJECT})

    assert cards.scoped("acme").git_config_row() is not None
    assert cards.scoped("default").git_config_row() is None


def test_the_mcp_tools_have_no_way_to_name_another_tenant(client):
    """Isolation by construction: the tools take no tenant, so an agent operates
    on its own partition or on nothing."""
    tools = {t["name"]: t for t in mcp.MCP_TOOLS if "git_config" in t["name"]}

    assert set(tools) == {
        "cfactory_get_git_config",
        "cfactory_set_git_config",
        "cfactory_verify_git_config",
    }
    for tool in tools.values():
        assert "tenant" not in tool["inputSchema"].get("properties", {})


def test_the_store_scope_is_what_a_config_read_follows(cards, settings):
    cards.scoped("acme").set_git_config(GitConfigUpdate(provider="github", project="acme/widgets"))

    assert cards.scoped("acme").git_target(settings).project == "acme/widgets"
    # Another tenant does not see it, and falls back to the environment instead.
    assert cards.scoped("globex").git_target(settings).project is None


# ── the one-release seed (the second mutation-check guard) ───────────────────


def _seeded_settings() -> Settings:
    return Settings(
        git_provider_token=_TEST_TOKEN,
        github_repo=_PROJECT,
        intake_project_id="proj-from-env",
    )


def test_the_seed_materialises_the_default_tenants_config_from_the_env(cards):
    """An existing single-tenant deployment keeps working with NO operator action,
    and its values become editable in the portal."""
    seeded = cards.seed_git_config_from_env(_seeded_settings())

    assert seeded is not None
    row = cards.git_config_row()
    assert row.tenant_id == "default"
    assert row.project == _PROJECT
    assert row.aifactory_project_id == "proj-from-env"


def test_the_seed_never_overwrites_a_stored_configuration(cards):
    """THE once-only rule. A seed that re-ran on every boot would silently undo
    every edit an operator made in the cockpit, which is the worst failure this
    feature has available to it."""
    cards.set_git_config(GitConfigUpdate(provider="gitlab", project="edited/by-a-human"))

    assert cards.seed_git_config_from_env(_seeded_settings()) is None

    row = cards.git_config_row()
    assert row.project == "edited/by-a-human"
    assert row.provider == "gitlab"


def test_a_second_boot_does_not_re_seed_either(cards):
    """The same rule stated as the sequence that actually happens: boot, edit,
    boot again."""
    settings = _seeded_settings()
    cards.seed_git_config_from_env(settings)
    cards.set_git_config(GitConfigUpdate(provider="github", project="edited/by-a-human"))

    cards.seed_git_config_from_env(settings)

    assert cards.git_config_row().project == "edited/by-a-human"


def test_nothing_to_seed_writes_no_row(cards):
    """A deploy that never configured any of this stays unconfigured rather than
    acquiring an empty row that reads as a deliberate choice."""
    assert cards.seed_git_config_from_env(Settings(git_provider_token=_TEST_TOKEN)) is None
    assert cards.git_config_row() is None


def test_a_tenant_with_no_row_still_resolves_from_the_env(cards):
    """The bridge itself: until the seed runs (or on a deploy that never boots
    it), resolution answers exactly what the environment said — which is why
    every pre-existing behaviour is unchanged."""
    target = cards.git_target(_seeded_settings())

    assert (target.project, target.aifactory_project_id) == (_PROJECT, "proj-from-env")
    assert target.stored is False


# ── the wiring: one source of truth ──────────────────────────────────────────


def test_the_stored_config_beats_the_env_for_every_consumer(cards, settings, monkeypatch):
    """The point of the phase. With BOTH set, the stored row wins everywhere —
    there is no consumer left reading the environment for a project."""
    monkeypatch.setattr(settings, "github_repo", "env/repo")
    monkeypatch.setattr(settings, "intake_project_id", "env-project")
    cards.set_git_config(
        GitConfigUpdate(
            provider="github", project="stored/repo", aifactory_project_id="stored-project"
        )
    )

    target = cards.git_target(settings)

    assert target.project == "stored/repo"
    assert target.import_project == "stored/repo"  # defaults to project (RFC-0020 §3.3)
    assert target.aifactory_project_id == "stored-project"


def test_a_card_opens_its_issue_in_the_tenants_configured_project(cards, ctx, host, settings):
    cards.set_git_config(GitConfigUpdate(provider="github", project="stored/repo"))
    host.body = {"number": 42, "title": "Widget throughput", "state": "open", "labels": []}

    card = card_ops.create_card(
        cards, ctx, CardCreate(title="Widget throughput", status="ready"), transport=host.transport()
    )

    assert card.issue_ref is not None
    assert card.issue_ref.startswith("stored/repo#")


def test_the_tenants_default_labels_land_on_the_issue_it_opens(cards, ctx, host):
    cards.set_git_config(
        GitConfigUpdate(provider="github", project="stored/repo", default_labels=["board", "triage"])
    )
    host.body = {"number": 42, "title": "Widget throughput", "state": "open", "labels": []}

    card_ops.create_card(
        cards, ctx, CardCreate(title="Widget throughput", status="ready"), transport=host.transport()
    )

    assert json.loads(host.requests[0].content)["labels"] == ["board", "triage"]


def test_the_import_reads_from_the_tenants_intake_project(cards, host, settings):
    """``intake_project`` is where a backfill READS from when it differs from the
    project the board opens issues in (RFC-0020 §3.3)."""
    cards.set_git_config(
        GitConfigUpdate(provider="github", project="stored/repo", intake_project="stored/legacy")
    )
    host.body = []

    result = issue_import.import_issues(cards, transport=host.transport())

    assert result["project"] == "stored/legacy"
    assert host.requests[0].url.path == "/repos/stored/legacy/issues"


def test_the_dispatch_target_comes_from_the_tenants_config(cards, settings):
    from cfactory.card_intake import aifactory_project_id

    cards.set_git_config(GitConfigUpdate(provider="github", aifactory_project_id="tenant-project"))

    assert aifactory_project_id(cards, settings) == "tenant-project"


# ── a GitLab configuration drives the GitLab provider, end to end ───────────


def test_a_gitlab_config_drives_the_gitlab_provider_end_to_end(cards, ctx, monkeypatch):
    """The panel is not a GitHub feature with three options in a dropdown: the
    stored provider + base_url select the canonical GitLab implementation, which
    then addresses the project GitLab's own way."""
    seen: list[httpx.Request] = []

    def _client(self: GitLabProvider) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "id": 901,
                    "iid": 7,
                    "title": "Widget throughput",
                    "description": "",
                    "state": "opened",
                    "labels": [],
                },
            )

        return httpx.AsyncClient(
            base_url=str(self._base_url), transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(GitLabProvider, "_client", _client)
    cards.set_git_config(
        GitConfigUpdate(
            provider="gitlab", base_url="https://gitlab.example.com", project="acme/widgets"
        )
    )

    card = card_ops.create_card(cards, ctx, CardCreate(title="Widget throughput", status="ready"))

    assert len(seen) == 1
    assert seen[0].url.host == "gitlab.example.com"
    assert seen[0].url.raw_path == b"/api/v4/projects/acme%2Fwidgets/issues"
    # The IID, not the row id — the identifier leak the provider layer prevents.
    assert card.issue_ref == "acme/widgets#7"


def test_verifying_a_gitlab_config_reads_the_gitlab_project(cards, ctx, monkeypatch):
    seen: list[httpx.Request] = []

    def _client(self: GitLabProvider) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"path_with_namespace": "acme/widgets"})

        return httpx.AsyncClient(
            base_url=str(self._base_url), transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(GitLabProvider, "_client", _client)
    cards.set_git_config(
        GitConfigUpdate(
            provider="gitlab", base_url="https://gitlab.example.com", project="acme/widgets"
        )
    )

    from cfactory.git_config_ops import verify_git_config

    result = verify_git_config(cards, ctx)

    assert result["ok"] is True
    assert result["repository"] == "acme/widgets"
    assert len(seen) == 1
    assert seen[0].url.raw_path == b"/api/v4/projects/acme%2Fwidgets"

"""Multiple git connections and repositories per tenant (RFC-0020 §3.3 phase 8, #373).

The limitation being removed: ``tenant_git_config`` held ONE row per tenant, so a
board got one provider against one repository and the three provider buttons in
Settings were a choice rather than three connections. A tenant now has many
connections (a provider + a host + a credential) and each connection has many
repositories, with exactly one repository per tenant marked as the default that a
card naming none resolves to.

Four properties are mutation-checked — break the guard and a named test here must
go red:

* **tenant isolation across connections** — a tenant cannot read, verify, edit or
  delete another tenant's connection or repository even by naming its id. Drop the
  tenant filter from the store lookups and
  ``test_another_tenants_connection_is_not_found_and_not_writable`` fails;
* **the credential is never returned or logged** — no list, verify, create or
  error payload carries it, on either surface. Let it out and
  ``test_no_read_surface_or_log_ever_returns_a_connection_credential`` fails;
* **the credential AAD binds the CONNECTION** — a sealed record lifted onto
  another of the same tenant's connections does not decrypt. Drop the connection
  from ``credentials._dek_aad`` / ``_kek_aad`` and
  ``test_a_sealed_record_moved_to_another_connection_does_not_decrypt`` fails;
* **one default repository per tenant** — enforced by the database, not by a
  check. Remove the unique ``default_for_tenant`` index and
  ``test_two_default_repositories_are_refused_by_the_database`` fails.

Plus the upgrade path end to end, in both shapes a real deployment takes: the
Alembic migration, and the boot-time adoption this service actually runs (it
bootstraps with ``create_all`` and may never run Alembic at all).
"""

from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest
from cfactory import (
    auth,
    config,
    credentials,
    git_config,
    git_config_ops,
    github_sync,
    issue_import,
    mcp,
)
from cfactory import cards as cards_module
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.card_intake import aifactory_project_id
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.config import Settings
from cfactory.credentials import (
    AAD_VERSION,
    LEGACY_AAD_VERSION,
    GitCredentialRow,
    Sealed,
    load_keyring,
    seal,
    unseal,
)
from cfactory.git_config import CREDENTIAL_MISSING, UNCONFIGURED, VERIFIED, GitConfigUpdate
from cfactory.git_connections import (
    GitConnectionCreate,
    GitConnectionUpdate,
    GitRepositoryCreate,
    GitRepositoryRow,
    GitRepositoryUpdate,
    GitResourceNotFoundError,
)
from cfactory.git_providers import HttpGitHubProvider, build_provider
from fastapi.testclient import TestClient
from runners.github.providers.gitlab_provider import GitLabProvider
from sqlalchemy import select, text

# Fake key material, pinned so a failure is reproducible. Not a secret: it
# protects nothing but this module's temp databases.
_KEY = base64.b64encode(b"p8" * 16).decode()
_ACTIVE = f"v1:{_KEY}"

_GH_SECRET = "ghp-GITHUB-CONNECTION-CREDENTIAL-1a2b"  # noqa: S105 — a fake, not a secret
_GL_SECRET = "glpat-GITLAB-CONNECTION-CREDENTIAL-3c4d"  # noqa: S105 — a fake, not a secret

_WRITER = "test-writer-key-not-a-credential"  # noqa: S105 — a fake, not a secret
_TENANT = "default"

_GH_PROJECT = "acme/widgets"
_GH_OTHER = "acme/gadgets"
_GL_PROJECT = "acme/group/pipelines"
_GL_OTHER = "acme/group/runners"
_GL_HOST = "https://gitlab.example.com"

_HTTP_BAD_REQUEST = 400
_HTTP_NOT_FOUND = 404
_HTTP_FORBIDDEN = 403


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="p8-test-hmac")  # noqa: S106 — a test fixture, not a secret


@pytest.fixture
def ctx(audit):
    return AuditContext(audit, "tester")


class FakeHost:
    """A stand-in git host recording every request, including its auth header."""

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"message": "denied"})
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "number": 11,
                    "title": "Widget throughput",
                    "body": "",
                    "state": "open",
                    "labels": [],
                    "html_url": "https://host.test/issues/11",
                },
            )
        return httpx.Response(200, json={"full_name": _GH_PROJECT, "default_branch": "main"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]

    def auth_headers(self) -> list[str]:
        """Every credential-bearing header value the host was sent."""
        return [
            value
            for request in self.requests
            for name, value in request.headers.items()
            if name.lower() in {"authorization", "private-token"}
        ]


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def settings():
    """A deployment with an encryption key and NO environment git credential, so
    what these tests exercise is each connection's own stored one."""
    return Settings(credential_key=_ACTIVE)


@pytest.fixture(autouse=True)
def _settings(monkeypatch, settings):
    for module in (cards_module, git_config, git_config_ops, github_sync, issue_import):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def keyring(settings):
    return load_keyring(settings)


@pytest.fixture
def client(cards, audit, host, monkeypatch, _settings):
    """One TestClient serving BOTH surfaces over one store (RFC-0019 §3.3)."""
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", host.transport)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.set_keys({_WRITER: {"read", "write"}})

    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = host.transport
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.reset_keystore()


def _connections_url(tenant: str = _TENANT) -> str:
    return f"/api/tenants/{tenant}/git-connections"


def _repositories_url(tenant: str = _TENANT) -> str:
    return f"/api/tenants/{tenant}/git-repositories"


def _call_tool(client, name: str, arguments: dict | None = None):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {_WRITER}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )


def _tool_payload(client, name: str, arguments: dict | None = None) -> dict:
    resp = _call_tool(client, name, arguments)
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


def _two_providers(store: CardStore) -> dict[str, int]:
    """A tenant with a GitHub connection and a GitLab one, two repositories each.

    The acceptance shape from #373, built through the store the ops layer uses, so
    every test below starts from the configuration a real tenant would have.
    """
    github = store.create_connection(GitConnectionCreate(provider="github", label="Work GitHub"))
    gitlab = store.create_connection(
        GitConnectionCreate(provider="gitlab", base_url=_GL_HOST, label="Self-hosted GitLab")
    )
    ids = {"github": github.id, "gitlab": gitlab.id}
    ids["gh_default"] = store.create_repository(
        github.id,
        GitRepositoryCreate(project=_GH_PROJECT, aifactory_project_id="gh-widgets-project"),
    ).id
    ids["gh_other"] = store.create_repository(
        github.id, GitRepositoryCreate(project=_GH_OTHER)
    ).id
    ids["gl_first"] = store.create_repository(
        gitlab.id,
        GitRepositoryCreate(project=_GL_PROJECT, aifactory_project_id="gl-pipelines-project"),
    ).id
    ids["gl_second"] = store.create_repository(
        gitlab.id, GitRepositoryCreate(project=_GL_OTHER, intake_project=_GL_PROJECT)
    ).id
    store.set_connection_credential(github.id, _GH_SECRET)
    store.set_connection_credential(gitlab.id, _GL_SECRET)
    return ids


# ── the shape #373 asks for ──────────────────────────────────────────────────


def test_two_connections_on_different_providers_each_with_two_repositories(cards, settings):
    """The acceptance criterion: two providers, four repositories, one default."""
    ids = _two_providers(cards)

    assert [c.provider for c in cards.connections()] == ["github", "gitlab"]
    assert len(cards.repositories()) == 4
    assert len(cards.repositories(ids["gitlab"])) == 2
    # The FIRST repository created is the tenant default; nothing else is.
    defaults = [repo.id for repo in cards.repositories() if repo.is_default]
    assert defaults == [ids["gh_default"]]

    gh = cards.git_target_for(settings, repository_id=ids["gh_other"])
    gl = cards.git_target_for(settings, repository_id=ids["gl_second"])
    assert (gh.provider, gh.base_url, gh.project) == (
        "github",
        "https://api.github.com",
        _GH_OTHER,
    )
    assert (gl.provider, gl.base_url, gl.project) == ("gitlab", _GL_HOST, _GL_OTHER)
    # Each repository is reached with ITS OWN connection's credential.
    assert gh.credential.token() == _GH_SECRET
    assert gl.credential.token() == _GL_SECRET
    # And the intake project falls back per repository, not per tenant.
    assert gl.import_project == _GL_PROJECT
    assert gh.import_project == _GH_OTHER


def test_the_same_host_cannot_be_configured_twice(client):
    first = client.post(_connections_url(), json={"provider": "github"})
    assert first.status_code == 200, first.text

    again = client.post(_connections_url(), json={"provider": "github"})

    assert again.status_code == _HTTP_BAD_REQUEST
    assert "already has a github connection" in again.json()["detail"]
    # Two hosts of the SAME provider are fine — that is the point of base_url.
    assert (
        client.post(
            _connections_url(), json={"provider": "github", "base_url": "https://ghe.acme.test"}
        ).status_code
        == 200
    )


def test_the_first_repository_is_the_default_even_when_not_asked_for(cards):
    connection = cards.create_connection(GitConnectionCreate(provider="github"))

    first = cards.create_repository(connection.id, GitRepositoryCreate(project=_GH_PROJECT))
    second = cards.create_repository(connection.id, GitRepositoryCreate(project=_GH_OTHER))

    assert first.is_default is True
    assert second.is_default is False


def test_setting_the_default_moves_it_rather_than_adding_one(cards, ids=None):
    ids = _two_providers(cards)

    cards.set_default_repository(ids["gl_second"])

    assert [repo.id for repo in cards.repositories() if repo.is_default] == [ids["gl_second"]]


def test_deleting_the_default_promotes_the_oldest_remaining_repository(cards):
    ids = _two_providers(cards)

    cards.delete_repository(ids["gh_default"])

    assert [repo.id for repo in cards.repositories() if repo.is_default] == [ids["gh_other"]]


def test_deleting_a_connection_takes_its_repositories_and_its_credential(cards, settings):
    ids = _two_providers(cards)

    cards.delete_connection(ids["github"])

    assert [c.id for c in cards.connections()] == [ids["gitlab"]]
    assert [repo.project for repo in cards.repositories()] == [_GL_PROJECT, _GL_OTHER]
    assert cards.credential_row(ids["github"]) is None
    # The tenant still has a default, so a card that names none still resolves.
    assert cards.default_repository().repository.project == _GL_PROJECT
    # And the surviving connection's credential is untouched.
    assert cards.connection_credential(ids["gitlab"], settings).token() == _GL_SECRET


def test_a_repository_patch_only_changes_what_it_sends(cards):
    ids = _two_providers(cards)

    cards.update_repository(ids["gl_second"], GitRepositoryUpdate(intake_project=None))

    repo = cards.repository(ids["gl_second"])
    assert repo.intake_project is None
    assert repo.project == _GL_OTHER


def test_moving_a_connection_clears_its_verification_but_keeps_its_credential(cards, settings):
    ids = _two_providers(cards)
    cards.record_connection_verification(ids["github"], error=None)
    assert cards.connection(ids["github"]).verified_at is not None

    cards.update_connection(ids["github"], GitConnectionUpdate(base_url="https://ghe.acme.test"))

    assert cards.connection(ids["github"]).verified_at is None
    # The credential is bound to the connection, not to its host, so it survives.
    assert cards.connection_credential(ids["github"], settings).token() == _GH_SECRET


def test_renaming_a_connection_does_not_clear_its_verification(cards):
    ids = _two_providers(cards)
    cards.record_connection_verification(ids["github"], error=None)

    cards.update_connection(ids["github"], GitConnectionUpdate(label="Renamed"))

    assert cards.connection(ids["github"]).verified_at is not None


# ── resolution: which repository a card means ────────────────────────────────


def test_a_card_with_no_repository_uses_the_tenant_default(cards, ctx, settings):
    ids = _two_providers(cards)
    card = cards.create(CardCreate(title="No repository named"))

    target = cards.git_target_for_card(card, settings)

    assert target.repository_id == ids["gh_default"]
    assert (target.project, target.provider) == (_GH_PROJECT, "github")
    assert target.credential.token() == _GH_SECRET


def test_a_card_that_names_a_repository_resolves_to_that_provider(cards, settings):
    ids = _two_providers(cards)
    card = cards.create(CardCreate(title="On GitLab", repository_id=ids["gl_first"]))

    target = cards.git_target_for_card(card, settings)

    assert (target.provider, target.base_url, target.project) == ("gitlab", _GL_HOST, _GL_PROJECT)
    assert target.credential.token() == _GL_SECRET
    # And the provider actually built for it is the canonical GitLab one, carrying
    # that connection's credential — not the tenant default's GitHub client.
    provider = build_provider(target, target.project)
    assert isinstance(provider, GitLabProvider)


def test_a_card_resolves_through_the_repository_its_issue_lives_in(cards, settings):
    """A card imported from one repo syncs back to THAT repo, even with no
    ``repository_id`` — its issue names the project, and the project names the
    repository."""
    ids = _two_providers(cards)
    card = cards.create(CardCreate(title="Imported", issue_ref=f"{_GH_OTHER}#4"))

    target = cards.git_target_for_card(card, settings)

    assert target.repository_id == ids["gh_other"]
    assert target.project == _GH_OTHER


def test_a_card_sync_opens_its_issue_on_its_own_connections_host(cards, ctx, monkeypatch):
    """Two GitHub connections, two hosts: the card's repository decides which host
    is called and which credential is sent."""
    public = cards.create_connection(GitConnectionCreate(provider="github"))
    enterprise = cards.create_connection(
        GitConnectionCreate(provider="github", base_url="https://ghe.acme.test")
    )
    cards.create_repository(public.id, GitRepositoryCreate(project=_GH_PROJECT))
    ghe_repo = cards.create_repository(enterprise.id, GitRepositoryCreate(project=_GH_OTHER))
    cards.set_connection_credential(public.id, _GH_SECRET)
    cards.set_connection_credential(enterprise.id, _GL_SECRET)
    host = FakeHost()
    card = cards.create(CardCreate(title="Widget throughput", repository_id=ghe_repo.id))

    result = github_sync.sync_card(cards, card, transport=host.transport())

    assert result["ok"] is True
    assert host.hosts() == ["ghe.acme.test"]
    assert host.auth_headers() == [f"Bearer {_GL_SECRET}"]


def test_the_aifactory_project_comes_from_the_cards_own_repository(cards, settings):
    """Two cards on one board can build into two different AIFactory projects."""
    ids = _two_providers(cards)
    default_card = cards.create(CardCreate(title="Default repo"))
    gitlab_card = cards.create(CardCreate(title="GitLab repo", repository_id=ids["gl_first"]))

    assert aifactory_project_id(cards, settings, default_card) == "gh-widgets-project"
    assert aifactory_project_id(cards, settings, gitlab_card) == "gl-pipelines-project"


def test_an_import_reads_one_repository_and_stamps_it_on_the_card(cards, ctx):
    """Phase 6's import is per-repository now: naming a repository decides the
    host, the credential and which repository the imported cards belong to."""
    ids = _two_providers(cards)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "number": 31,
                    "title": "From the other repo",
                    "body": "",
                    "state": "open",
                    "labels": [],
                    "updated_at": "2026-07-01T10:00:00Z",
                }
            ],
        )

    result = issue_import.import_issues(
        cards, repository_id=ids["gh_other"], transport=httpx.MockTransport(handler)
    )

    assert result["ok"] is True
    assert result["project"] == _GH_OTHER
    assert result["imported"] == 1
    assert seen[0].headers["authorization"] == f"Bearer {_GH_SECRET}"
    imported = cards.get_by_issue_ref(f"{_GH_OTHER}#31")
    assert imported.repository_id == ids["gh_other"]


def test_a_tenant_with_no_repositories_still_resolves_from_the_environment(cards, settings):
    """The phase-2 bridge is untouched: no connections at all means the deployment's
    environment variables answer, exactly as before this phase."""
    settings.github_repo = "env/repo"
    settings.git_provider_token = "env-token-not-a-credential"  # noqa: S105 — a fake

    target = cards.git_target(settings)

    assert (target.project, target.stored) == ("env/repo", False)
    assert target.credential.info.source == "env"


# ── the REST + MCP surface ───────────────────────────────────────────────────


def test_the_rest_surface_lists_connections_with_their_repositories(client, cards):
    ids = _two_providers(cards)

    body = client.get(_connections_url()).json()

    assert [c["provider"] for c in body["connections"]] == ["github", "gitlab"]
    assert body["default_repository_id"] == ids["gh_default"]
    gitlab = body["connections"][1]
    assert gitlab["base_url"] == _GL_HOST
    assert gitlab["label"] == "Self-hosted GitLab"
    assert [r["project"] for r in gitlab["repositories"]] == [_GL_PROJECT, _GL_OTHER]
    # A connection with repositories and a credential is reachable in principle,
    # and nothing has proved it yet.
    assert gitlab["status"] == "configured"


def test_a_connection_with_no_repositories_reads_as_unconfigured(client):
    created = client.post(_connections_url(), json={"provider": "github"}).json()

    assert created["status"] == UNCONFIGURED
    assert created["repositories"] == []
    verified = client.post(f"{_connections_url()}/{created['id']}:verify").json()
    assert verified["ok"] is False
    assert verified["status"] == UNCONFIGURED


def test_a_connection_without_a_credential_reads_as_credential_missing(client):
    created = client.post(_connections_url(), json={"provider": "github"}).json()
    client.post(
        f"{_connections_url()}/{created['id']}/repositories", json={"project": _GH_PROJECT}
    )

    body = client.get(_connections_url()).json()["connections"][0]

    assert body["status"] == CREDENTIAL_MISSING
    assert body["credential"]["configured"] is False
    assert body["credential"]["source"] == "none"


def test_verifying_a_connection_records_it_and_reads_one_repository(client, cards, host):
    ids = _two_providers(cards)

    body = client.post(f"{_connections_url()}/{ids['github']}:verify").json()

    assert body["ok"] is True
    assert body["verified_project"] == _GH_PROJECT
    assert body["connection"]["status"] == VERIFIED
    # Exactly one provider call, and it carried that connection's credential.
    assert len(host.requests) == 1
    assert host.auth_headers() == [f"Bearer {_GH_SECRET}"]


def test_a_failed_verify_is_recorded_on_the_connection_not_the_tenant(client, cards, host):
    ids = _two_providers(cards)
    host.status_code = _HTTP_NOT_FOUND

    body = client.post(f"{_connections_url()}/{ids['github']}:verify").json()

    assert body["ok"] is False
    assert "404" in body["reason"]
    assert "404" in cards.connection(ids["github"]).verify_error
    # The other connection is unaffected — a failure is per-connection.
    assert cards.connection(ids["gitlab"]).verify_error is None


def test_the_mcp_twins_operate_on_the_same_connections(client, cards):
    """RFC-0019 §3.3 parity: one implementation, two transports."""
    created = _tool_payload(
        client, "cfactory_create_git_connection", {"provider": "gitlab", "base_url": _GL_HOST}
    )
    _tool_payload(
        client,
        "cfactory_create_git_repository",
        {"connection_id": created["id"], "project": _GL_PROJECT},
    )

    rest = client.get(_connections_url()).json()
    assert [c["id"] for c in rest["connections"]] == [created["id"]]
    assert [r["project"] for r in rest["connections"][0]["repositories"]] == [_GL_PROJECT]
    tools = _tool_payload(client, "cfactory_list_git_repositories")
    assert [r["project"] for r in tools["repositories"]] == [_GL_PROJECT]
    assert tools["default_repository_id"] == rest["connections"][0]["repositories"][0]["id"]


def test_an_unknown_connection_is_a_404_over_rest_and_an_error_over_mcp(client):
    assert client.delete(f"{_connections_url()}/4242").status_code == _HTTP_NOT_FOUND
    assert client.post(f"{_connections_url()}/4242:verify").status_code == _HTTP_NOT_FOUND

    payload = _tool_payload(client, "cfactory_delete_git_connection", {"connection_id": 4242})

    assert "no git connection 4242" in payload["error"]


def test_a_repository_path_the_provider_cannot_address_is_refused(client):
    created = client.post(_connections_url(), json={"provider": "azure_devops"}).json()

    bad = client.post(
        f"{_connections_url()}/{created['id']}/repositories", json={"project": "owner/repo"}
    )

    assert bad.status_code == _HTTP_BAD_REQUEST
    assert "organization/project/repo" in bad.json()["detail"]


def test_an_intake_trigger_label_is_still_refused_per_repository(client):
    created = client.post(_connections_url(), json={"provider": "github"}).json()

    bad = client.post(
        f"{_connections_url()}/{created['id']}/repositories",
        json={"project": _GH_PROJECT, "default_labels": ["factory:hard"]},
    )

    assert bad.status_code == _HTTP_BAD_REQUEST
    assert "intake trigger" in bad.json()["detail"]


def test_every_connection_mutation_is_audit_chained(client, cards, audit):
    created = client.post(_connections_url(), json={"provider": "github"}).json()
    client.post(f"{_connections_url()}/{created['id']}/repositories", json={"project": _GH_PROJECT})
    client.delete(f"{_connections_url()}/{created['id']}")

    kinds = [(e.kind, e.ok) for e in audit.list()]

    assert ("create_git_connection", True) in kinds
    assert ("create_git_repository", True) in kinds
    assert ("delete_git_connection", True) in kinds
    assert audit.verify() == [], "the tamper-evident chain must still be intact"


# ── the single-configuration shim (the old panel keeps working) ──────────────


def test_the_legacy_endpoints_read_and_write_the_default_repository(client, cards):
    """The phase-2 panel is still served: its PUT lands on the tenant's default
    repository and its GET reads the same values back."""
    put = client.put(
        f"/api/tenants/{_TENANT}/git-config",
        json={
            "provider": "gitlab",
            "base_url": _GL_HOST,
            "project": _GL_PROJECT,
            "aifactory_project_id": "legacy-project",
        },
    )
    assert put.status_code == 200, put.text

    body = client.get(f"/api/tenants/{_TENANT}/git-config").json()
    assert (body["provider"], body["base_url"], body["project"]) == ("gitlab", _GL_HOST, _GL_PROJECT)
    assert body["aifactory_project_id"] == "legacy-project"
    # And it wrote ONE connection with ONE default repository, not a second
    # source of truth.
    resolved = cards.default_repository()
    assert resolved.connection.provider == "gitlab"
    assert resolved.repository.project == _GL_PROJECT
    assert len(cards.connections()) == 1


def test_the_legacy_credential_endpoint_targets_the_default_connection(client, cards, settings):
    client.put(
        f"/api/tenants/{_TENANT}/git-config",
        json={"provider": "github", "project": _GH_PROJECT},
    )

    client.put(f"/api/tenants/{_TENANT}/git-credential", json={"credential": _GH_SECRET})

    connection = cards.default_repository().connection
    assert cards.connection_credential(connection.id, settings).token() == _GH_SECRET
    assert client.get(f"/api/tenants/{_TENANT}/git-config").json()["status"] == "configured"


def test_the_legacy_put_edits_the_connection_rather_than_stranding_the_credential(
    client, cards, settings
):
    """A credential stored before a host was chosen must not be orphaned by
    choosing one: the shim MOVES the tenant's single connection."""
    client.put(f"/api/tenants/{_TENANT}/git-credential", json={"credential": _GH_SECRET})

    client.put(
        f"/api/tenants/{_TENANT}/git-config",
        json={"provider": "github", "base_url": "https://ghe.acme.test", "project": _GH_PROJECT},
    )

    assert len(cards.connections()) == 1
    target = cards.git_target(settings)
    assert target.base_url == "https://ghe.acme.test"
    assert target.credential.token() == _GH_SECRET


def test_clearing_the_legacy_project_leaves_the_repositories_alone(client, cards):
    ids = _two_providers(cards)

    client.put(f"/api/tenants/{_TENANT}/git-config", json={"provider": "github"})

    # No default any more, so a card that names none is unconfigured...
    assert client.get(f"/api/tenants/{_TENANT}/git-config").json()["status"] == UNCONFIGURED
    assert cards.default_repository() is None
    # ...but nothing a human configured was destroyed.
    assert len(cards.repositories()) == 4
    assert cards.repository(ids["gl_first"]).project == _GL_PROJECT


# ── GUARD (a): tenant isolation across connections ──────────────────────────


def test_another_tenants_connection_is_not_found_and_not_writable(cards, settings):
    """MUTATION GUARD (isolation): every connection and repository lookup filters
    on the tenant. Drop that filter and this fails."""
    acme, globex = cards.scoped("acme"), cards.scoped("globex")
    connection = acme.create_connection(GitConnectionCreate(provider="github"))
    repository = acme.create_repository(connection.id, GitRepositoryCreate(project=_GH_PROJECT))
    acme.set_connection_credential(connection.id, _GH_SECRET)

    # Reads see nothing of it.
    assert globex.connections() == []
    assert globex.repositories() == []
    assert globex.default_repository() is None
    assert globex.credential_row(connection.id) is None
    # Naming the id directly is NOT FOUND, over every operation.
    for call in (
        lambda: globex.connection(connection.id),
        lambda: globex.repository(repository.id),
        lambda: globex.update_connection(connection.id, GitConnectionUpdate(label="stolen")),
        lambda: globex.delete_connection(connection.id),
        lambda: globex.delete_repository(repository.id),
        lambda: globex.set_default_repository(repository.id),
        lambda: globex.set_connection_credential(connection.id, "not-mine"),
        lambda: globex.clear_connection_credential(connection.id),
        lambda: globex.record_connection_verification(connection.id, error=None),
    ):
        with pytest.raises(GitResourceNotFoundError):
            call()
    # And acme's configuration is intact afterwards.
    assert acme.connection(connection.id).label == "github"
    assert acme.connection_credential(connection.id, settings).token() == _GH_SECRET


def test_a_tenant_cannot_reach_another_tenants_connection_over_http(cards, client, monkeypatch):
    """The URL tenant is checked, not trusted (RFC-0020 §3.3): a request scoped to
    one tenant cannot address another's, whatever it types in the path."""
    resp = client.get(_connections_url("acme"))

    assert resp.status_code == _HTTP_FORBIDDEN
    assert "not your tenant" in resp.json()["detail"]


def test_the_mcp_connection_tools_take_no_tenant_argument(client):
    """Isolation by construction: there is no field an agent could put another
    tenant's name in."""
    tools = [t for t in mcp.MCP_TOOLS if "git_connection" in t["name"] or "git_repositor" in t["name"]]

    assert len(tools) == 12
    for tool in tools:
        assert "tenant" not in tool["inputSchema"].get("properties", {})


# ── GUARD (b): the credential is never returned, never logged ────────────────


def test_no_read_surface_or_log_ever_returns_a_connection_credential(client, cards, caplog):
    """MUTATION GUARD (redaction): let a credential into a response body, an error,
    an audit entry or a log record and this fails."""
    caplog.set_level(logging.DEBUG)
    ids = _two_providers(cards)

    bodies = [
        client.get(_connections_url()).text,
        client.get(_repositories_url()).text,
        client.post(f"{_connections_url()}/{ids['github']}:verify").text,
        client.put(
            f"{_connections_url()}/{ids['gitlab']}/credential", json={"credential": _GL_SECRET}
        ).text,
        client.delete(f"{_connections_url()}/{ids['gitlab']}/credential").text,
        client.patch(
            f"{_connections_url()}/{ids['github']}", json={"label": "Renamed"}
        ).text,
        # Both MCP surfaces, including the error paths.
        _call_tool(client, "cfactory_list_git_connections").text,
        _call_tool(client, "cfactory_list_git_repositories").text,
        _call_tool(client, "cfactory_verify_git_connection", {"connection_id": ids["github"]}).text,
        _call_tool(
            client,
            "cfactory_set_git_connection_credential",
            {"connection_id": ids["github"], "credential": _GH_SECRET},
        ).text,
        _call_tool(client, "cfactory_delete_git_connection", {"connection_id": 4242}).text,
    ]

    for body in bodies:
        assert _GH_SECRET not in body
        assert _GL_SECRET not in body
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert _GH_SECRET not in logged
    assert _GL_SECRET not in logged
    # Nor does the sealed record render it, which is what keeps it out of a traceback.
    assert _GH_SECRET not in repr(cards.sealed_for(ids["github"]))


def test_nothing_stored_on_a_connection_contains_the_credential(cards, ids=None):
    ids = _two_providers(cards)
    row = cards.credential_row(ids["github"])

    stored = "|".join(
        str(getattr(row, column.name)) for column in GitCredentialRow.__table__.columns
    )

    assert _GH_SECRET not in stored
    for encoding in (base64.b64encode(_GH_SECRET.encode()).decode(), _GH_SECRET.encode().hex()):
        assert encoding not in stored


# ── GUARD (c): the AAD binds the connection ─────────────────────────────────


def test_a_sealed_record_moved_to_another_connection_does_not_decrypt(cards, keyring, settings):
    """MUTATION GUARD (replay): the connection is associated data on BOTH crypto
    layers, so a record lifted from one of a tenant's connections onto another does
    not decrypt. Drop the connection from ``_dek_aad`` / ``_kek_aad`` and this
    fails — the record would then be replayable across every connection the tenant
    has, which is the whole point of binding it."""
    ids = _two_providers(cards)
    github = ids["github"]
    # A third connection with NO credential of its own — the one a stolen record
    # would be replayed onto.
    other = cards.create_connection(
        GitConnectionCreate(provider="github", base_url="https://ghe.acme.test")
    ).id
    sealed = cards.sealed_for(github)

    # The crypto refuses it outright...
    with pytest.raises(credentials.CredentialError, match="did not decrypt"):
        unseal(sealed, tenant=_TENANT, connection=other, keyring=keyring)

    # ...and so does the store when the row itself is moved, which is the attack:
    # database write access, no key, one row re-pointed.
    with cards._session.begin() as session:  # noqa: SLF001 — simulating a tampered row
        row = session.scalars(
            select(GitCredentialRow).where(GitCredentialRow.connection_id == github)
        ).one()
        row.connection_id = other
    assert cards.connection_credential(other, settings).token() is None


def test_a_record_from_another_tenant_still_does_not_decrypt(keyring):
    """The phase-3 property, unchanged: the tenant is bound in as well."""
    sealed = seal(_GH_SECRET, tenant="acme", connection=1, keyring=keyring)

    with pytest.raises(credentials.CredentialError, match="did not decrypt"):
        unseal(sealed, tenant="globex", connection=1, keyring=keyring)


def test_the_connection_binding_cannot_be_asked_for_weakly(keyring):
    """A caller cannot request the legacy tenant-only binding: it is chosen from the
    stored ``aad_version`` and nothing else."""
    sealed = seal(_GH_SECRET, tenant=_TENANT, connection=3, keyring=keyring)

    assert sealed.aad_version == AAD_VERSION
    # Claiming to be legacy does not make the ciphertext readable that way either —
    # the tag is over the binding it was actually sealed with.
    lying = Sealed(sealed.key_version, sealed.wrapped_key, sealed.ciphertext, LEGACY_AAD_VERSION)
    with pytest.raises(credentials.CredentialError):
        unseal(lying, tenant=_TENANT, connection=3, keyring=keyring)


# ── GUARD (d): one default repository per tenant ────────────────────────────


def test_two_default_repositories_are_refused_by_the_database(cards):
    """MUTATION GUARD (single default): the constraint is a UNIQUE index on
    ``default_for_tenant``, not an application check — so a second default is
    refused even by a write that goes straight to the table. Remove the index and
    this fails, and "which repository does a card with none resolve to?" stops
    having one answer."""
    ids = _two_providers(cards)
    assert cards.repository(ids["gh_default"]).is_default is True

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with cards._session.begin() as session:  # noqa: SLF001 — the point is to bypass the store
            row = session.get(GitRepositoryRow, ids["gl_first"])
            row.default_for_tenant = _TENANT

    # Still exactly one, and it is the original.
    assert [repo.id for repo in cards.repositories() if repo.is_default] == [ids["gh_default"]]


def test_two_tenants_each_have_their_own_default(cards):
    """The constraint is per tenant, not global — two tenants must both have one."""
    for tenant in ("acme", "globex"):
        scoped = cards.scoped(tenant)
        connection = scoped.create_connection(GitConnectionCreate(provider="github"))
        scoped.create_repository(connection.id, GitRepositoryCreate(project=f"{tenant}/widgets"))

    assert cards.scoped("acme").default_repository().repository.project == "acme/widgets"
    assert cards.scoped("globex").default_repository().repository.project == "globex/widgets"


# ── the upgrade path, end to end ────────────────────────────────────────────


def _write_legacy_config(store: CardStore, tenant: str = _TENANT, **fields) -> None:
    """A pre-phase-8 ``tenant_git_config`` row, as a phase-2/3 deployment has."""
    values = {
        "provider": "github",
        "base_url": None,
        "project": _GH_PROJECT,
        "intake_project": None,
        "aifactory_project_id": "legacy-aifactory-project",
        "verified_at": None,
        "verify_error": None,
        "credential_rejected": None,
        **fields,
    }
    with store._session.begin() as session:  # noqa: SLF001 — writing the OLD shape on purpose
        session.execute(
            text(
                "INSERT INTO tenant_git_config (tenant_id, provider, base_url, project, "
                "intake_project, aifactory_project_id, default_labels, verified_at, "
                "verify_error, credential_rejected, created_at, updated_at) VALUES "
                "(:tenant, :provider, :base_url, :project, :intake_project, "
                ":aifactory_project_id, '[\"board\"]', :verified_at, :verify_error, "
                ":credential_rejected, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"tenant": tenant, **values},
        )


def _write_legacy_credential(store: CardStore, secret: str, keyring, tenant: str = _TENANT) -> None:
    """A credential sealed the pre-phase-8 way: bound to the tenant, no connection.

    Built with the same crypto the old release used — the legacy branch of the AAD
    helpers — because the whole point of the test is that a record written by that
    release still works.
    """
    key_version, kek = keyring.active
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    dek = AESGCM.generate_key(bit_length=256)
    sealed = Sealed(
        key_version=key_version,
        wrapped_key=credentials._encrypt(  # noqa: SLF001 — reproducing the old release
            kek, dek, credentials._kek_aad(tenant, key_version, None)  # noqa: SLF001
        ),
        ciphertext=credentials._encrypt(  # noqa: SLF001
            dek, secret.encode(), credentials._dek_aad(tenant, None)  # noqa: SLF001
        ),
        aad_version=LEGACY_AAD_VERSION,
    )
    # Raw SQL naming only the columns the OLD table had, so this writes the same
    # row on a create_all database and on one still at the phase-3 revision.
    with store._session.begin() as session:  # noqa: SLF001
        session.execute(
            text(
                "INSERT INTO tenant_git_credential (tenant_id, key_version, wrapped_key, "
                "ciphertext, created_at, updated_at) VALUES (:tenant, :key_version, "
                ":wrapped_key, :ciphertext, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "tenant": tenant,
                "key_version": sealed.key_version,
                "wrapped_key": sealed.wrapped_key,
                "ciphertext": sealed.ciphertext,
            },
        )


def test_a_pre_phase_8_tenant_is_adopted_and_still_resolves_and_verifies(
    cards, ctx, keyring, settings, host
):
    """THE upgrade test. A tenant whose credential was sealed by phase 3, whose
    configuration was a single row, must resolve and verify after the upgrade with
    no operator action — anything less is an outage, not a test failure."""
    _write_legacy_config(cards, verified_at=None)
    _write_legacy_credential(cards, _GH_SECRET, keyring)

    assert cards.adopt_legacy_git_config(settings) == 1

    # One connection, one repository, marked default, every field preserved.
    resolved = cards.default_repository()
    assert (resolved.connection.provider, resolved.connection.base_url) == ("github", "")
    assert resolved.repository.project == _GH_PROJECT
    assert resolved.repository.aifactory_project_id == "legacy-aifactory-project"
    assert resolved.repository.default_labels == ["board"]
    # The credential came with it, re-sealed onto the connection binding.
    row = cards.credential_row(resolved.connection.id)
    assert row.aad_version == AAD_VERSION
    target = cards.git_target(settings)
    assert target.credential.token() == _GH_SECRET
    # And a verify now reaches the host with it.
    result = git_config_ops.verify_git_config(cards, ctx, transport=host.transport())
    assert result["ok"] is True
    assert host.auth_headers() == [f"Bearer {_GH_SECRET}"]


def test_the_adopted_verify_state_survives_the_upgrade(cards, keyring, settings):
    """A tenant that was VERIFIED before the upgrade is still verified after it —
    the adoption copies the proof, it does not ask for it again."""
    from datetime import UTC, datetime

    stamp = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
    _write_legacy_config(cards, verified_at=stamp)
    _write_legacy_credential(cards, _GH_SECRET, keyring)

    cards.adopt_legacy_git_config(settings)

    assert cards.git_target(settings).status == VERIFIED


def test_adoption_is_idempotent_and_never_overwrites_an_edit(cards, keyring, settings):
    """It runs on every boot, so a second pass must do nothing — and must not undo
    a change made in the cockpit since the first."""
    _write_legacy_config(cards)
    _write_legacy_credential(cards, _GH_SECRET, keyring)
    cards.adopt_legacy_git_config(settings)
    connection = cards.default_repository().connection
    cards.create_repository(connection.id, GitRepositoryCreate(project=_GH_OTHER))
    cards.set_default_repository(cards.repositories()[1].id)

    assert cards.adopt_legacy_git_config(settings) == 0

    assert len(cards.connections()) == 1
    assert [repo.project for repo in cards.repositories()] == [_GH_PROJECT, _GH_OTHER]
    assert cards.default_repository().repository.project == _GH_OTHER


def test_every_tenant_is_adopted_not_only_the_stores_own(cards, keyring, settings):
    """An upgrade must not wait for a tenant to log in."""
    _write_legacy_config(cards, tenant="acme", project="acme/widgets")
    _write_legacy_config(cards, tenant="globex", project="globex/gadgets")
    _write_legacy_credential(cards, _GH_SECRET, keyring, tenant="globex")

    assert cards.adopt_legacy_git_config(settings) == 2

    assert cards.scoped("acme").default_repository().repository.project == "acme/widgets"
    globex = cards.scoped("globex")
    assert globex.git_target(settings).credential.token() == _GH_SECRET
    # And the credential did not leak sideways: acme has none of its own.
    assert globex.default_repository().connection.id != (
        cards.scoped("acme").default_repository().connection.id
    )
    assert cards.scoped("acme").git_target(settings).credential.configured is False


def test_a_credential_adopted_without_the_key_keeps_working_when_the_key_returns(
    cards, keyring, settings
):
    """A missing key must not destroy a credential. The record keeps its
    pre-phase-8 binding, is unreadable while the key is gone (as it already was),
    and is re-sealed on the first read once the key is back."""
    _write_legacy_config(cards)
    _write_legacy_credential(cards, _GH_SECRET, keyring)
    keyless = Settings(credential_key=None)

    cards.adopt_legacy_git_config(keyless)

    connection = cards.default_repository().connection
    assert cards.credential_row(connection.id).aad_version == LEGACY_AAD_VERSION
    assert cards.connection_credential(connection.id, keyless).token() is None
    # The key comes back: the legacy record reads, and is migrated on that read.
    assert cards.connection_credential(connection.id, settings).token() == _GH_SECRET
    assert cards.credential_row(connection.id).aad_version == AAD_VERSION


def test_the_alembic_migration_adopts_the_single_row(tmp_path, monkeypatch, keyring):
    """The other upgrade shape: a deployment that runs migrations rather than
    bootstrapping with ``create_all``. The DDL and the data copy are exercised on a
    real database, from the phase-3 revision to head."""
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    # ``migrations/env.py`` resolves its own URL through ``cfactory.db``, so that is
    # what has to point at the temp database — an environment variable would be
    # read through the CACHED settings and miss.
    from cfactory import db as db_module

    monkeypatch.setattr(db_module, "get_settings", lambda: Settings(database_url=url))
    cfg = Config(str(_alembic_ini()))
    command.upgrade(cfg, "d5e83a1c9f22")

    store = CardStore(url, create=False)
    _write_legacy_config(store, project="acme/legacy")
    _write_legacy_credential(store, _GH_SECRET, keyring)

    command.upgrade(cfg, "head")

    resolved = store.default_repository()
    assert resolved.repository.project == "acme/legacy"
    assert resolved.repository.default_labels == ["board"]
    assert resolved.connection.provider == "github"
    # The credential moved to the connection and is still the pre-phase-8 binding,
    # because a migration has no key to re-seal with — the app does that at boot.
    row = store.credential_row(resolved.connection.id)
    assert row.connection_id == resolved.connection.id
    assert row.aad_version == LEGACY_AAD_VERSION
    # And it reads, which is the property that matters: no outage.
    assert store.connection_credential(resolved.connection.id, Settings(credential_key=_ACTIVE)).token() == _GH_SECRET


def _alembic_ini():
    from pathlib import Path

    import cfactory

    return Path(cfactory.__file__).resolve().parent.parent / "alembic.ini"


def test_an_old_schema_database_gains_the_new_columns_at_boot(tmp_path, keyring, settings):
    """THE production upgrade path: this service bootstraps its schema with
    ``create_all``, which never ALTERs an existing table, so the live SQLite file
    from the previous release starts with a ``tenant_git_credential`` that has no
    ``connection_id``, no ``aad_version``, and a UNIQUE index on ``tenant_id`` —
    one credential per tenant, which is the limitation being removed.

    Building a store over that file has to add the two columns, drop that index,
    and leave the sealed record intact and readable.
    """
    from sqlalchemy import create_engine

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        # The pre-phase-8 shape, verbatim.
        conn.execute(
            text(
                "CREATE TABLE tenant_git_config (id INTEGER PRIMARY KEY, tenant_id VARCHAR(64) "
                "NOT NULL DEFAULT 'default', provider VARCHAR(32) NOT NULL DEFAULT 'github', "
                "base_url VARCHAR(512), project VARCHAR(256), intake_project VARCHAR(256), "
                "aifactory_project_id VARCHAR(128), default_labels JSON, verified_at TIMESTAMP, "
                "verify_error VARCHAR(512), credential_rejected BOOLEAN, created_at TIMESTAMP, "
                "updated_at TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX ix_tenant_git_config_tenant ON tenant_git_config (tenant_id)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE tenant_git_credential (id INTEGER PRIMARY KEY, tenant_id "
                "VARCHAR(64) NOT NULL DEFAULT 'default', key_version VARCHAR(32) NOT NULL, "
                "wrapped_key BLOB NOT NULL, ciphertext BLOB NOT NULL, created_at TIMESTAMP, "
                "updated_at TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX ix_tenant_git_credential_tenant "
                "ON tenant_git_credential (tenant_id)"
            )
        )
    engine.dispose()

    # The boot sequence: create_all + the late-column guard, then the adoption.
    store = CardStore(url)
    _write_legacy_config(store, project="acme/live")
    _write_legacy_credential(store, _GH_SECRET, keyring)
    assert store.adopt_legacy_git_config(settings) == 1

    resolved = store.default_repository()
    assert resolved.repository.project == "acme/live"
    assert store.git_target(settings).credential.token() == _GH_SECRET
    # The per-tenant index is gone, so the tenant can hold a second connection's
    # credential — which is the whole point of the phase.
    second = store.create_connection(GitConnectionCreate(provider="gitlab", base_url=_GL_HOST))
    store.set_connection_credential(second.id, _GL_SECRET)
    assert store.connection_credential(second.id, settings).token() == _GL_SECRET
    assert store.connection_credential(resolved.connection.id, settings).token() == _GH_SECRET

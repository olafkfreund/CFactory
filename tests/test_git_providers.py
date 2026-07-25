"""The board is genuinely provider-agnostic (RFC-0020 phase 1).

``test_github_sync.py`` proves the Phase 6 contract still holds; it passes
unmodified through the protocol, which is itself the point. This module proves
the other half: that the contract is not GitHub's. The same board code is pointed
at **GitLab** and must behave identically while the host underneath differs in
every way that could leak:

* the identifier is an IID, not the issue's ``id`` — a card must record ``#7``
  (the IID) and never ``#901`` (the row id GitLab also sends);
* the state vocabulary is ``opened``/``closed``, not ``open``/``closed``;
* labels arrive as flat strings, not ``{"name": ...}`` objects;
* the API is ``/api/v4/projects/{urlencoded}/issues``, not ``/repos/...``.

None of that reaches card logic — the canonical provider normalises it — and
these tests fail if any of it starts to.

Everything runs against a MOCKED provider: the GitLab tests drive the REAL
vendored ``GitLabProvider`` (so its request-building and parsing are under test)
with only its HTTP client swapped for an ``httpx.MockTransport``. Nothing here
touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from cfactory import card_ops, github_sync
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.config import Settings
from cfactory.git_providers import (
    SUPPORTED_PROVIDERS,
    HttpGitHubProvider,
    IssueProvider,
    build_provider,
)
from runners.github.providers.azure_devops_provider import AzureDevOpsProvider
from runners.github.providers.gitlab_provider import GitLabProvider
from runners.github.providers.protocol import ProviderType

from cfactory.audit import AuditStore  # isort: skip

# Not a credential: a fake token so `sync_enabled` is on inside these tests.
_TEST_TOKEN = "test-gitlab-token-not-a-credential"  # noqa: S105 — a fake, not a secret
_PROJECT = "acme/widgets"
# GitLab sends BOTH: `iid` is what a human and a URL name the issue by, `id` is a
# global row id. Picking the wrong one is the classic provider leak, so they are
# deliberately different here.
_IID = 7
_ROW_ID = 901


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def ctx(tmp_path):
    return AuditContext(
        AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="git-providers-test-hmac"),  # noqa: S106 — a test fixture, not a secret
        "tester",
    )


class FakeGitLab:
    """A stand-in GitLab issues API, speaking GitLab's own JSON."""

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.issue: dict[str, object] = {
            "id": _ROW_ID,
            "iid": _IID,
            "title": "Widget throughput",
            "description": "",
            "state": "opened",
            "labels": [],
            "web_url": f"https://gitlab.test/{_PROJECT}/-/issues/{_IID}",
        }
        self.posts: list[httpx.Request] = []
        self.gets: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posts.append(request)
        else:
            self.gets.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"message": "boom"})
        return httpx.Response(200, json=self.issue)


@pytest.fixture
def gitlab(monkeypatch):
    """The real vendored GitLabProvider with only its socket faked.

    Patching ``_client`` rather than passing a transport is deliberate: the
    canonical provider constructs its own ``AsyncClient`` and we do not edit
    vendored code to add a seam. Everything above the socket — URL building,
    project-path encoding, response parsing — is the code under test.
    """
    fake = FakeGitLab()

    def _client(self: GitLabProvider) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://gitlab.test",
            headers=self._headers,
            transport=httpx.MockTransport(fake.handler),
        )

    monkeypatch.setattr(GitLabProvider, "_client", _client)
    return fake


@pytest.fixture
def settings():
    return Settings(
        git_provider="gitlab",
        git_provider_token=_TEST_TOKEN,
        git_provider_url="https://gitlab.test",
        github_repo=_PROJECT,
    )


@pytest.fixture(autouse=True)
def _synced(monkeypatch, settings):
    monkeypatch.setattr(github_sync, "get_settings", lambda: settings)
    return settings


def _make(cards, ctx, **fields):
    data = {"title": "Widget throughput", "acceptance_criteria": ["p99 < 100ms"], **fields}
    return card_ops.create_card(cards, ctx, CardCreate(**data))


# ── provider selection (config, not code) ────────────────────────────────────


def test_github_is_the_default_so_existing_deploys_are_unchanged():
    """A deploy that only ever set CFACTORY_GITHUB_TOKEN keeps the Phase 6 path."""
    provider = build_provider(Settings(github_token=_TEST_TOKEN), _PROJECT)

    assert isinstance(provider, HttpGitHubProvider)
    assert provider.provider_type is ProviderType.GITHUB
    assert provider.repo == _PROJECT


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("gitlab", GitLabProvider), ("GitLab", GitLabProvider), ("github", HttpGitHubProvider)],
)
def test_the_provider_type_is_selectable_by_config(kind, expected):
    provider = build_provider(Settings(git_provider=kind, git_provider_token=_TEST_TOKEN), _PROJECT)

    assert isinstance(provider, expected)


def test_azure_devops_gets_its_three_part_path():
    provider = build_provider(
        Settings(git_provider="azure_devops", git_provider_token=_TEST_TOKEN),
        "contoso/payments/widgets",
    )

    assert isinstance(provider, AzureDevOpsProvider)
    assert (provider._org, provider._proj, provider.repo) == ("contoso", "payments", "widgets")


def test_an_unaddressable_azure_path_is_a_config_error_not_a_bad_call():
    with pytest.raises(ValueError, match="organization/project/repo"):
        build_provider(
            Settings(git_provider="azure_devops", git_provider_token=_TEST_TOKEN), _PROJECT
        )


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="unknown git provider"):
        build_provider(Settings(git_provider="bitbucket", git_provider_token=_TEST_TOKEN), _PROJECT)
    assert "bitbucket" not in SUPPORTED_PROVIDERS


def test_every_selectable_provider_satisfies_the_board_protocol():
    """The segregation is checked, not assumed: what the board asks of a provider
    is a subset of what the canonical protocol already promises."""
    for kind in SUPPORTED_PROVIDERS:
        project = "org/project/repo" if kind == "azure_devops" else _PROJECT
        settings = Settings(git_provider=kind, git_provider_token=_TEST_TOKEN)
        assert isinstance(build_provider(settings, project), IssueProvider), kind


# ── the explicit opt-in survives the rewiring (RFC-0019 §3.5) ────────────────


@pytest.mark.parametrize("ambient", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_an_ambient_gh_login_does_not_switch_issue_writing_on(monkeypatch, ambient):
    """The credential that opens issues in someone's repo must be *chosen*.

    ``gh auth login`` leaves these in the environment of every developer machine
    and CI runner; inheriting one would turn the board's issue-writing on by
    accident, which is exactly what happened the first time this was wired.
    """
    monkeypatch.setenv(ambient, "ambient-not-ours")
    monkeypatch.delenv("CFACTORY_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CFACTORY_GIT_PROVIDER_TOKEN", raising=False)

    assert github_sync.sync_enabled(Settings()) is False


def test_either_token_setting_switches_it_on():
    assert github_sync.sync_enabled(Settings(github_token=_TEST_TOKEN)) is True
    assert github_sync.sync_enabled(Settings(git_provider_token=_TEST_TOKEN)) is True


# ── card -> GitLab issue ─────────────────────────────────────────────────────


def test_a_ready_card_opens_a_gitlab_issue(cards, ctx, gitlab):
    card = _make(cards, ctx, status="ready")

    assert len(gitlab.posts) == 1
    # GitLab addresses a project by URL-encoded path, not by GitHub's /repos/.
    assert gitlab.posts[0].url.raw_path == b"/api/v4/projects/acme%2Fwidgets/issues"
    assert card.github_sync_error is None


def test_the_card_records_the_iid_not_the_row_id(cards, ctx, gitlab):
    """The identifier leak this whole layer exists to prevent."""
    card = _make(cards, ctx, status="ready")

    assert len(gitlab.posts) == 1
    assert card.issue_ref == f"{_PROJECT}#{_IID}"
    assert str(_ROW_ID) not in (card.issue_ref or "")


def test_gitlabs_state_vocabulary_does_not_reach_the_card(cards, ctx, gitlab):
    """GitLab says "opened"; the board's vocabulary is open/closed, and the card
    stays in the planning column the planner put it in."""
    card = _make(cards, ctx, status="ready")

    assert gitlab.issue["state"] == "opened"  # what the host said ...
    assert card.issue_state == "open"  # ... and what the board records.
    assert card.status == "ready"


def test_the_issue_is_opened_without_labels(cards, ctx, gitlab):
    """RFC-0011's factory:<tier> intake triggers on a label, and the card is
    already being dispatched — a labelled issue would build it a second time.
    Provider-independent: the GitLab path must not sneak labels in either."""
    _make(cards, ctx, status="ready")

    assert "labels" not in json.loads(gitlab.posts[0].content)


def test_a_backlog_card_opens_nothing(cards, ctx, gitlab):
    card = _make(cards, ctx, status="backlog")

    assert gitlab.posts == []
    assert card.issue_ref is None


# ── GitLab issue -> card ─────────────────────────────────────────────────────


def test_closing_the_issue_in_gitlab_moves_the_card_to_done(cards, ctx, gitlab):
    card = _make(cards, ctx, status="ready")
    gitlab.issue |= {"state": "closed"}

    result = card_ops.sync_card_github(cards, ctx, card.card_key)

    assert result["card"]["status"] == "done"
    assert result["card"]["issue_state"] == "closed"


def test_gitlab_flat_labels_mirror_down(cards, ctx, gitlab):
    """GitLab sends ["bug"], GitHub sends [{"name": "bug"}] — the card gets the
    same thing from both."""
    card = _make(cards, ctx, status="ready")
    gitlab.issue |= {"title": "Renamed in GitLab", "labels": ["bug", "p1"]}

    result = card_ops.sync_card_github(cards, ctx, card.card_key)

    assert result["card"]["title"] == "Renamed in GitLab"
    assert result["card"]["labels"] == ["bug", "p1"]


def test_the_provider_wins_on_gitlab_too(cards, ctx, gitlab):
    """Phase 6's conflict rule, unchanged, on a host that is not GitHub."""
    card = _make(cards, ctx, status="ready", tier="medium", priority=3, milestone="v2")
    cards.update(
        card.card_key,
        {"title": "Local rename", "status": "done", "acceptance_criteria": ["local AC"]},
    )
    gitlab.issue |= {"title": "GitLab rename", "state": "opened", "labels": ["bug"]}

    after = card_ops.sync_card_github(cards, ctx, card.card_key)["card"]

    # Mirrored fields: the provider's value, not the local one.
    assert after["title"] == "GitLab rename"
    assert after["labels"] == ["bug"]
    assert after["issue_state"] == "open"
    # Reopened upstream, so the board must stop claiming the work is finished.
    assert after["status"] == "in_progress"
    # Planning-only fields: the board's own, untouched by the sync.
    assert after["priority"] == 3
    assert after["tier"] == "medium"
    assert after["milestone"] == "v2"
    assert after["acceptance_criteria"] == ["local AC"]


def test_syncing_twice_opens_one_gitlab_issue(cards, ctx, gitlab):
    """Idempotency is a property of the board, not of the host: issue_ref
    non-NULL means 'already has an issue' whoever hosts it."""
    card = _make(cards, ctx, status="ready")

    card_ops.sync_card_github(cards, ctx, card.card_key)
    card_ops.sync_card_github(cards, ctx, card.card_key)

    assert len(gitlab.posts) == 1, "a re-sync must not open a second issue"
    assert cards.get(card.card_key).issue_ref == f"{_PROJECT}#{_IID}"


# ── failure stays fail-safe behind the protocol ──────────────────────────────


def test_a_gitlab_outage_is_surfaced_not_swallowed(cards, ctx, monkeypatch):
    outage = FakeGitLab(status_code=500)
    monkeypatch.setattr(
        GitLabProvider,
        "_client",
        lambda _self: httpx.AsyncClient(
            base_url="https://gitlab.test", transport=httpx.MockTransport(outage.handler)
        ),
    )

    card = _make(cards, ctx, status="ready")

    assert card.issue_ref is None, "a failed sync must not claim an issue exists"
    assert "500" in (card.github_sync_error or "")


def test_a_malformed_provider_payload_never_raises(cards, ctx, monkeypatch):
    """The provider layer is third-party code: a response the canonical parser
    chokes on (no `iid` -> KeyError) must still be a marked card, not a 500."""
    monkeypatch.setattr(
        GitLabProvider,
        "_client",
        lambda _self: httpx.AsyncClient(
            base_url="https://gitlab.test",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"no": "iid"})),
        ),
    )

    card = _make(cards, ctx, status="ready")

    assert card.issue_ref is None
    assert "KeyError" in (card.github_sync_error or "")

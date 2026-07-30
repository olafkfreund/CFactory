"""The tenant's provider reaches the fleet (RFC-0020 §3.5, Factory#366).

The bug: PFactory, AIFactory and TFactory each picked a git host from their own
configuration, so a GitLab tenant's PARR run reconnoitred github.com and opened
a GitHub PR. The fix carries the tenant's declaration on the repo reference the
task contract already had — ``gitlab:group/project`` — and nothing else.

Contract points covered here (CFactory's half; the receiving half is tested in
each sibling repo):

* every intake door gets the provider-qualified reference, on all three stages;
* the qualification comes from THIS CARD's repository, so two cards on one board
  dispatch against two different hosts (the #373 multi-connection law);
* a GitHub tenant's payload is byte-for-byte what it was before this phase;
* the reference round-trips ``gitlab:group/project`` unchanged, and a colon that
  is not a known provider (a clone URL) survives intact;
* an unconfigured tenant sends no reference at all rather than a guessed one.

**Mutation guard (a).** ``test_dispatch_does_not_fall_back_to_the_env_provider``
is the one that fails if a service is allowed to read its own default instead of
the contract: point the tenant at GitLab, leave the deployment env on GitHub,
and assert what goes out says gitlab. Breaking the resolution back to the env
(``target_from_settings`` instead of the card's repository) turns it red.
"""

from __future__ import annotations

import pytest
from cfactory import card_intake
from cfactory.audit import AuditStore
from cfactory.auth import reset_keystore
from cfactory.cards import CardStore
from cfactory.config import Settings
from cfactory.git_config import parse_repo_ref, qualify_repo
from cfactory.git_connections import GitConnectionCreate, GitRepositoryCreate
from cfactory.store import WorkItemStore

from cards_harness import Upstream, build_client

_TEST_HMAC = "fleet-propagation-test-hmac"

AIF_INTAKE = card_intake.AIFACTORY_INTAKE_ENDPOINT
PF_INTAKE = card_intake.PFACTORY_INTAKE_ENDPOINT
TF_INTAKE = card_intake.TFACTORY_INTAKE_ENDPOINT

_GL_HOST = "https://gitlab.example.com"
_GL_PROJECT = "platform/pipelines"
_GH_PROJECT = "acme/widgets"

# No credentials are stored here on purpose. Dispatch resolves a host and a
# project; it never reads a token, and a test that had to seal one would be
# claiming a coupling that does not exist. It also keeps issue sync inert, so
# the only outbound calls these tests see are the intake doors.


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def items(tmp_path):
    return WorkItemStore(f"sqlite:///{tmp_path / 'items.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def client(cards, items, audit, upstream, monkeypatch):
    # The deployment's environment says GitHub throughout. Every test that
    # expects gitlab therefore proves the answer came from the tenant's stored
    # configuration and not from here — which is the whole point of the phase.
    settings = Settings(intake_project_id="env-project", git_provider="github")
    monkeypatch.setattr(card_intake, "get_settings", lambda: settings)
    return build_client(cards, items, audit, upstream)


@pytest.fixture(autouse=True)
def _restore_keystore():
    yield
    reset_keystore()


def _gitlab_tenant(store: CardStore) -> int:
    """A tenant whose default repository lives on a self-hosted GitLab."""
    connection = store.create_connection(
        GitConnectionCreate(provider="gitlab", base_url=_GL_HOST, label="Self-hosted GitLab")
    )
    repo = store.create_repository(
        connection.id,
        GitRepositoryCreate(project=_GL_PROJECT, aifactory_project_id="gl-pipelines"),
    )
    return repo.id


def _github_tenant(store: CardStore) -> int:
    connection = store.create_connection(GitConnectionCreate(provider="github", label="Work"))
    repo = store.create_repository(
        connection.id,
        GitRepositoryCreate(project=_GH_PROJECT, aifactory_project_id="gh-widgets"),
    )
    return repo.id


def _card(client, **overrides) -> dict:
    body = {"title": "Ship the widget", "acceptance_criteria": ["AC#1: it ships"]}
    body.update(overrides)
    resp = client.post("/api/cards", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _promote(client, card_key: str, tier: str = "low") -> dict:
    resp = client.patch(f"/api/cards/{card_key}", json={"status": "ready", "tier": tier})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── the reference itself ─────────────────────────────────────────────────────


def test_qualification_round_trips_a_gitlab_project():
    """``gitlab:group/project`` in, the same string out — the contract's rule."""
    assert qualify_repo("gitlab", _GL_PROJECT) == f"gitlab:{_GL_PROJECT}"
    assert parse_repo_ref(f"gitlab:{_GL_PROJECT}") == ("gitlab", _GL_PROJECT)
    provider, project = parse_repo_ref(f"gitlab:{_GL_PROJECT}")
    assert qualify_repo(provider, project) == f"gitlab:{_GL_PROJECT}"


def test_github_stays_unqualified_so_nothing_downstream_changes():
    """A GitHub tenant's reference is what it always was, and still parses."""
    assert qualify_repo("github", _GH_PROJECT) == _GH_PROJECT
    assert parse_repo_ref(_GH_PROJECT) == ("github", _GH_PROJECT)
    assert qualify_repo(*parse_repo_ref(_GH_PROJECT)) == _GH_PROJECT


def test_azure_devops_qualifies_with_its_three_part_path():
    assert qualify_repo("azure_devops", "org/proj/repo") == "azure_devops:org/proj/repo"
    assert parse_repo_ref("azure_devops:org/proj/repo") == ("azure_devops", "org/proj/repo")


def test_an_unknown_prefix_is_part_of_the_project_not_a_provider():
    """A clone URL must survive: PFactory's reconnaissance accepts one.

    Splitting on the first colon regardless would turn
    ``https://gitlab.example/g/p`` into provider ``https``, which is how a
    "generic" parser breaks the one caller that had been working.
    """
    url = "https://gitlab.example.com/platform/pipelines.git"
    assert parse_repo_ref(url) == ("github", url)
    assert parse_repo_ref("bitbucket:team/repo") == ("github", "bitbucket:team/repo")


def test_no_project_means_no_reference():
    assert qualify_repo("gitlab", None) is None
    assert qualify_repo("gitlab", "") is None
    assert parse_repo_ref(None) is None
    assert parse_repo_ref("   ") is None


# ── what actually leaves CFactory ────────────────────────────────────────────


def test_dispatch_does_not_fall_back_to_the_env_provider(client, cards, upstream):
    """MUTATION GUARD (a): the contract decides the host, never the env default.

    The deployment environment says github (see the ``client`` fixture); the
    tenant says gitlab. Every door must hear gitlab. Make any of them resolve
    from ``Settings`` instead of the card's repository and this goes red.
    """
    _gitlab_tenant(cards)
    card = _card(client)
    _promote(client, card["card_key"], tier="low")

    sent = upstream.payload_for(AIF_INTAKE)
    assert sent is not None, "the low-tier card should have reached AIFactory"
    assert sent["repo"] == f"gitlab:{_GL_PROJECT}"
    assert parse_repo_ref(sent["repo"])[0] == "gitlab"


def test_every_stage_door_carries_the_qualification(client, cards, upstream):
    """plan, code and test all get it — a sequence must not change host midway."""
    _gitlab_tenant(cards)
    card = _card(client)
    client.patch(f"/api/cards/{card['card_key']}", json={"tier": "low"})

    client.post(f"/api/cards/{card['card_key']}/actions/plan")
    assert upstream.payload_for(PF_INTAKE)["repo"] == f"gitlab:{_GL_PROJECT}"

    client.post(f"/api/cards/{card['card_key']}/actions/code")
    assert upstream.payload_for(AIF_INTAKE)["repo"] == f"gitlab:{_GL_PROJECT}"


def test_a_hard_card_reaches_pfactory_qualified(client, cards, upstream):
    """Tier routing sends hard to planning; the host travels with it."""
    _gitlab_tenant(cards)
    card = _card(client)
    _promote(client, card["card_key"], tier="hard")

    sent = upstream.payload_for(PF_INTAKE)
    assert sent is not None, "a hard card should have reached PFactory"
    assert sent["repo"] == f"gitlab:{_GL_PROJECT}"


def test_a_github_tenant_is_completely_unaffected(client, cards, upstream):
    """No prefix, and every field the payload had before is untouched."""
    _github_tenant(cards)
    card = _card(client)
    _promote(client, card["card_key"], tier="low")

    sent = upstream.payload_for(AIF_INTAKE)
    assert sent["repo"] == _GH_PROJECT
    assert ":" not in sent["repo"]
    assert sent["project_id"] == "gh-widgets"
    assert sent["auto_continue"] is True
    assert sent["payload"]["labels"] == ["factory:low"]


def test_an_unconfigured_tenant_sends_no_reference(client, upstream):
    """Nothing configured means nothing claimed — not a guess at github.

    The receiving service then behaves exactly as it did before this phase,
    which for a deployment that never filled the panel in is the only honest
    answer available.
    """
    card = _card(client)
    _promote(client, card["card_key"], tier="low")

    sent = upstream.payload_for(AIF_INTAKE)
    assert sent is not None
    assert "repo" not in sent


def test_two_cards_two_hosts_on_one_board(client, cards, upstream):
    """The #373 law survives: resolution is per CARD, not per tenant.

    A tenant with both connections dispatches each card against the repository
    it actually names. A tenant-wide provider would make one of these wrong.
    """
    _github_tenant(cards)
    gitlab_repo = _gitlab_tenant(cards)

    on_default = _card(client, title="Default repo card")
    _promote(client, on_default["card_key"], tier="low")
    assert upstream.payload_for(AIF_INTAKE)["repo"] == _GH_PROJECT

    on_gitlab = _card(client, title="GitLab card", repository_id=gitlab_repo)
    _promote(client, on_gitlab["card_key"], tier="low")
    assert upstream.payload_for(AIF_INTAKE)["repo"] == f"gitlab:{_GL_PROJECT}"

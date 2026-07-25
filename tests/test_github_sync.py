"""Card <-> GitHub issue sync (RFC-0019 Phase 6, §3.5, #302).

Every test here runs against a MOCKED GitHub — an ``httpx.MockTransport``
injected through the same ``action_transport_dep`` seam the intake dispatch
uses. Nothing in this file touches the network.

RFC-0020 phase 1 moved the board onto the fleet's ``GitProvider`` protocol, and
this module is **unchanged** by that — which is the strongest statement available
that Phase 6's contract survived the rewiring intact: the same assertions, over
the same GitHub REST calls, now travel through the protocol. The proof that the
contract is not GitHub-specific is next door in ``test_git_providers.py``.

Contract points covered:

* a card opens an issue and records the reference (card -> issue);
* a card carrying an ``issue_ref`` ADOPTS that issue — no POST, no duplicate
  (this is the guard the first mutation check weakens);
* an issue closed/renamed/relabelled on GitHub mirrors onto the card
  (issue -> card), so the board stops lying about work that moved;
* a genuine conflict — both sides edited — resolves GITHUB WINS on the mirrored
  fields while the card's planning fields survive untouched (this is the guard
  the second mutation check weakens);
* a GitHub outage is surfaced (``github_sync_error`` on the card, ``ok=False``,
  an audit entry) rather than swallowed — and never a 500;
* syncing twice is idempotent: one issue, one POST.
"""

from __future__ import annotations

import httpx
import pytest
from cfactory import auth, card_ops, config, github_sync, mcp
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.config import Settings
from cfactory.github_sync import IssueRef, sync_card
from fastapi.testclient import TestClient

from cfactory.api_deps import action_transport_dep  # isort: skip

# Not a credential: a fake token so `sync_enabled` is on inside these tests.
_TEST_TOKEN = "test-github-token-not-a-credential"  # noqa: S105 — a fake, not a secret
_TEST_HMAC = "github-sync-test-hmac"
_WRITER = "writer-key"
_REPO = "acme/widgets"


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)


@pytest.fixture
def ctx(audit):
    return AuditContext(audit, "tester")


class FakeGitHub:
    """A stand-in issues API: records every call and answers from its own state."""

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.issue = {"number": 42, "title": "Widget throughput", "state": "open", "labels": []}
        self.posts: list[httpx.Request] = []
        self.gets: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                self.posts.append(request)
            else:
                self.gets.append(request)
            if self.status_code != 200:
                return httpx.Response(self.status_code, json={"message": "boom"})
            return httpx.Response(200, json=self.issue)

        return httpx.MockTransport(handler)


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def settings():
    return Settings(github_token=_TEST_TOKEN, github_repo=_REPO, github_api_url="https://gh.test")


@pytest.fixture(autouse=True)
def _synced(monkeypatch, settings):
    """Every test in this module runs with GitHub sync CONFIGURED — the module is
    about what sync does, and the unconfigured posture has its own test that
    re-patches this. Autouse so no test has to remember to ask for it."""
    monkeypatch.setattr(github_sync, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def client(cards, audit, github, _synced, monkeypatch):
    """One TestClient serving BOTH surfaces over one pair of stores and one fake
    GitHub, so REST and MCP can be compared directly (RFC-0019 §3.3). MCP
    resolves its own collaborators, so its seams are patched rather than
    overridden."""
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", github.transport)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.set_keys({_WRITER: {"read", "write"}})

    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = github.transport
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.reset_keystore()


def _make(cards, ctx, github, **fields):
    """A card created with the GitHub hook wired to the fake."""
    data = {"title": "Widget throughput", "acceptance_criteria": ["p99 < 100ms"], **fields}
    return card_ops.create_card(cards, ctx, CardCreate(**data), transport=github.transport())


# ── issue_ref parsing (it lands in a URL path, so it is a trust boundary) ────


@pytest.mark.parametrize(
    "text",
    ["acme/widgets#42", " acme/widgets#42 ", "a.b-c/d_e#1"],
)
def test_issue_ref_round_trips(text):
    ref = IssueRef.parse(text)
    assert ref is not None
    assert str(ref) == text.strip()


@pytest.mark.parametrize(
    "text",
    [None, "", "acme/widgets", "42", "../../etc#1", "https://github.com/acme/widgets/issues/1"],
)
def test_malformed_issue_ref_is_rejected(text):
    assert IssueRef.parse(text) is None


# ── card -> issue ────────────────────────────────────────────────────────────


def test_ready_card_opens_an_issue_and_records_it(cards, ctx, github):
    card = _make(cards, ctx, github, status="ready")

    assert len(github.posts) == 1
    assert github.posts[0].url.path == f"/repos/{_REPO}/issues"
    assert card.issue_ref == f"{_REPO}#42"
    assert card.issue_state == "open"
    assert card.github_sync_error is None


def test_a_backlog_card_opens_nothing(cards, ctx, github):
    """Planning stays private until it is ready — GitHub is where work lives."""
    card = _make(cards, ctx, github, status="backlog")

    assert github.posts == []
    assert card.issue_ref is None


def test_sync_is_off_when_unconfigured(cards, ctx, github, monkeypatch):
    """No token = no network call, whatever the card does. The default posture."""
    monkeypatch.setattr(github_sync, "get_settings", Settings)

    card = _make(cards, ctx, github, status="ready")

    assert github.posts == [] and github.gets == []
    assert card.issue_ref is None


# ── adoption + idempotency ───────────────────────────────────────────────────


def test_adopting_an_existing_issue_creates_no_duplicate(cards, ctx, github):
    """The card names an issue somebody already filed: adopt it, never POST."""
    card = _make(cards, ctx, github, status="ready", issue_ref="acme/widgets#7")

    assert github.posts == []
    assert [r.url.path for r in github.gets] == ["/repos/acme/widgets/issues/7"]
    assert card.issue_ref == "acme/widgets#7"


def test_syncing_twice_opens_one_issue(cards, ctx, github):
    """Idempotency: issue_ref non-NULL means 'already has an issue', so the
    second sync adopts and mirrors instead of filing a duplicate."""
    card = _make(cards, ctx, github, status="ready")
    first = card.issue_ref

    card_ops.sync_card_github(cards, ctx, card.card_key, transport=github.transport())
    card_ops.sync_card_github(cards, ctx, card.card_key, transport=github.transport())

    assert len(github.posts) == 1, "a re-sync must not open a second issue"
    assert cards.get(card.card_key).issue_ref == first


# ── issue -> card mirroring ──────────────────────────────────────────────────


def test_closing_the_issue_on_github_moves_the_card_to_done(cards, ctx, github):
    card = _make(cards, ctx, github, status="ready")
    github.issue |= {"state": "closed"}

    result = card_ops.sync_card_github(cards, ctx, card.card_key, transport=github.transport())

    assert result["card"]["status"] == "done"
    assert result["card"]["issue_state"] == "closed"


def test_title_and_labels_mirror_down_from_the_issue(cards, ctx, github):
    card = _make(cards, ctx, github, status="ready")
    github.issue |= {"title": "Renamed on GitHub", "labels": [{"name": "bug"}, {"name": "p1"}]}

    result = card_ops.sync_card_github(cards, ctx, card.card_key, transport=github.transport())

    assert result["card"]["title"] == "Renamed on GitHub"
    assert result["card"]["labels"] == ["bug", "p1"]


# ── conflict resolution: GitHub wins (RFC-0019 §3.5) ─────────────────────────


def test_github_wins_when_both_sides_changed(cards, ctx, github):
    """The whole point of Phase 6.

    Both sides edited since the last sync: the planner renamed the card and
    called it done; GitHub says something else and the issue is still open.
    GitHub is the record of truth, so ITS values land — and the card's
    planning-only fields survive, because GitHub has no opinion on them.
    """
    card = _make(cards, ctx, github, status="ready", tier="medium", priority=3, milestone="v2")
    cards.update(
        card.card_key,
        {"title": "Local rename", "status": "done", "acceptance_criteria": ["local AC"]},
    )
    github.issue |= {"title": "GitHub rename", "state": "open", "labels": [{"name": "bug"}]}

    result = card_ops.sync_card_github(cards, ctx, card.card_key, transport=github.transport())
    after = result["card"]

    # Mirrored fields: GitHub's value, not the local one.
    assert after["title"] == "GitHub rename"
    assert after["labels"] == ["bug"]
    assert after["issue_state"] == "open"
    # Reopened upstream, so the board must stop claiming the work is finished.
    assert after["status"] == "in_progress"
    # Planning-only fields: the board's own, untouched by the sync.
    assert after["priority"] == 3
    assert after["tier"] == "medium"
    assert after["milestone"] == "v2"
    assert after["acceptance_criteria"] == ["local AC"]


def test_closed_issue_wins_over_a_card_still_in_progress(cards, ctx, github):
    card = _make(cards, ctx, github, status="ready")
    cards.update(card.card_key, {"status": "in_progress"})
    github.issue |= {"state": "closed"}

    result = card_ops.sync_card_github(cards, ctx, card.card_key, transport=github.transport())

    assert result["card"]["status"] == "done"


# ── failure is surfaced, never swallowed ─────────────────────────────────────


def test_github_failure_marks_the_card_and_reports_not_ok(cards, ctx):
    broken = FakeGitHub(status_code=500)
    card = _make(cards, ctx, broken, status="ready")

    assert card.issue_ref is None, "a failed sync must not claim an issue exists"
    assert "500" in (card.github_sync_error or "")


def test_github_outage_does_not_500_the_board(client, cards):
    """A transport-level outage on the explicit endpoint: 200 with ok=False."""

    def dead(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    card = cards.create(CardCreate(title="Widget throughput"))
    client.app.dependency_overrides[action_transport_dep] = lambda: httpx.MockTransport(dead)

    resp = client.post(f"/api/cards/{card.card_key}/sync-github")

    assert resp.status_code == 200
    assert resp.json()["sync"]["ok"] is False
    assert "connection refused" in resp.json()["card"]["github_sync_error"]


def test_a_failed_sync_is_audited_as_not_ok(cards, ctx, audit):
    """The audit chain records the failure — there is no gap where a sync was
    attempted and nothing was written."""
    broken = FakeGitHub(status_code=502)
    card = cards.create(CardCreate(title="Widget throughput"))

    card_ops.sync_card_github(cards, ctx, card.card_key, transport=broken.transport())

    assert [(e.kind, e.ok) for e in audit.list()] == [("sync_card_github", False)]


def test_sync_never_raises_even_on_a_nonsense_response(cards):
    """The never-raises contract, asserted at the module boundary."""
    garbage = httpx.MockTransport(lambda _r: httpx.Response(200, json={"no": "number"}))
    card = cards.create(CardCreate(title="Widget throughput"))

    result = sync_card(cards, card, transport=garbage)

    assert result["ok"] is False
    assert cards.get(card.card_key).github_sync_error


def test_a_recovered_sync_clears_the_error(cards, ctx):
    broken = FakeGitHub(status_code=500)
    card = _make(cards, ctx, broken, status="ready")
    assert card.github_sync_error

    healthy = FakeGitHub()
    result = card_ops.sync_card_github(cards, ctx, card.card_key, transport=healthy.transport())

    assert result["card"]["github_sync_error"] is None
    assert result["card"]["issue_ref"] == f"{_REPO}#42"


# ── both surfaces (RFC-0019 §3.3 — the parity law) ───────────────────────────


def test_rest_and_mcp_sync_the_same_way(client, cards, github):
    """The REST endpoint and the MCP tool are the same operation."""
    rest_card = cards.create(CardCreate(title="Widget throughput"))
    mcp_card = cards.create(CardCreate(title="Widget throughput"))

    rest = client.post(f"/api/cards/{rest_card.card_key}/sync-github").json()
    # A second card cannot adopt the SAME issue — RFC-0020 §3.6's unique
    # (tenant, issue_ref) index is what makes the import idempotent, and it
    # binds every writer, not only the importer. So the fake opens a fresh
    # issue for the second card, which is what a real host would do.
    github.issue = {**github.issue, "number": 43}
    mcp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "cfactory_sync_card_github",
                "arguments": {"card_key": mcp_card.card_key},
            },
        },
    ).json()

    assert rest["sync"]["ok"] is True and rest["sync"]["created"] is True
    assert '"ok": true' in mcp["result"]["content"][0]["text"]
    assert len(github.posts) == 2


def test_syncing_an_unknown_card_404s(client):
    assert client.post("/api/cards/FCT-999/sync-github").status_code == 404


def test_an_issue_ref_the_card_never_had_is_adopted_over_rest(client, cards, github):
    card = cards.create(CardCreate(title="Widget throughput"))

    client.patch(f"/api/cards/{card.card_key}", json={"issue_ref": "acme/widgets#9"})

    assert github.posts == []
    assert cards.get(card.card_key).issue_ref == "acme/widgets#9"

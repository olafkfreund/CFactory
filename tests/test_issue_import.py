"""Importing a repository's existing issues into the board (RFC-0020 §3.6, #368).

Every test runs against a MOCKED provider — an ``httpx.MockTransport`` through
the same ``action_transport_dep`` seam the card sync uses. Nothing here touches
the network, and one test drives the whole import over the canonical **GitLab**
provider to prove the feature is genuinely provider-agnostic rather than a GitHub
feature wearing a protocol.

The three properties that carry weight, each with a test named for it:

* an imported card is never ``ready`` (importing a repo must not dispatch a build
  per issue — this is the guard the second mutation check weakens);
* import is idempotent (re-running updates the same cards; two concurrent runs
  produce one card — the guard the first mutation check weakens);
* a pull request never becomes a card.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cfactory import auth, card_ops, config, github_sync, issue_import, mcp
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.card_intake import maybe_dispatch
from cfactory.card_ops import AuditContext, StageRefusedError
from cfactory.cards import CardCreate, CardStore, DuplicateIssueRefError
from cfactory.config import Settings
from cfactory.issue_import import import_issues
from fastapi.testclient import TestClient
from runners.github.providers.gitlab_provider import GitLabProvider

from cfactory.api_deps import action_transport_dep  # isort: skip

_TEST_TOKEN = "test-provider-token-not-a-credential"  # noqa: S105 — a fake, not a secret
_TEST_HMAC = "issue-import-test-hmac"
_WRITER = "writer-key"
_REPO = "acme/widgets"

_UPDATED = "2026-07-20T10:00:00Z"


def _issue(number: int, **overrides) -> dict:
    """One GitHub issue payload, with the fields the mapping reads."""
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": f"Body of {number}",
        "state": "open",
        "labels": [],
        "assignees": [],
        "milestone": None,
        "user": {"login": "someone"},
        "created_at": _UPDATED,
        "updated_at": _UPDATED,
        "html_url": f"https://github.com/{_REPO}/issues/{number}",
        **overrides,
    }


class FakeHost:
    """A stand-in issues API. Records every request and answers from its list."""

    def __init__(self, issues: list[dict] | None = None, *, status_code: int = 200) -> None:
        self.issues = issues if issues is not None else [_issue(1), _issue(2)]
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.status_code != 200:
                return httpx.Response(self.status_code, json={"message": "boom"})
            # Page 2+ is always empty: these fixtures are smaller than a page.
            page = int(request.url.params.get("page", "1"))
            if request.url.path.endswith(f"/{_REPO}/issues") and page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=self.issues)

        return httpx.MockTransport(handler)

    @property
    def list_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("issues")]


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)


@pytest.fixture
def ctx(audit):
    return AuditContext(audit, "tester")


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def settings():
    return Settings(github_token=_TEST_TOKEN, github_repo=_REPO, github_api_url="https://gh.test")


@pytest.fixture(autouse=True)
def _configured(monkeypatch, settings):
    """Every test runs with the provider CONFIGURED; the unconfigured posture has
    its own test that re-patches this."""
    monkeypatch.setattr(github_sync, "get_settings", lambda: settings)
    monkeypatch.setattr(issue_import, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def client(cards, audit, host, _configured, monkeypatch):
    """One TestClient serving BOTH surfaces over one store and one fake host, so
    REST and MCP can be compared directly (RFC-0019 §3.3)."""
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
    auth.set_keys({})


def _import(cards_store, host_, **kwargs):
    return import_issues(cards_store, transport=host_.transport(), **kwargs)


# ── Backfill ────────────────────────────────────────────────────────────────


def test_backfill_turns_every_open_issue_into_a_card(cards, host):
    result = _import(cards, host)

    assert result["ok"] is True
    assert result["imported"] == 2
    titles = {c.title for c in cards.list()}
    assert titles == {"Issue 1", "Issue 2"}


def test_backfill_asks_for_open_issues_and_never_for_prs(cards, host):
    _import(cards, host)

    params = host.list_calls[0].url.params
    assert params.get("state") == "open"
    # `include_prs` is pinned False in the filters, and GitHub's /issues endpoint
    # answers with PRs regardless — which is why the provider drops them.
    assert "labels" not in params


def test_a_pull_request_never_becomes_a_card(cards):
    host = FakeHost([_issue(1), _issue(2, pull_request={"url": "…"})])

    result = _import(cards, host)

    assert result["imported"] == 1
    assert [c.title for c in cards.list()] == ["Issue 1"]


def test_truncation_is_reported_never_silent(cards, monkeypatch, settings):
    monkeypatch.setattr(settings, "import_max", 2)
    host = FakeHost([_issue(n) for n in range(1, 6)])

    result = _import(cards, host)

    assert result["truncated"] is True
    assert result["import_max"] == 2
    assert result["seen"] == 2


def test_an_unreachable_provider_is_reported_not_raised(cards):
    host = FakeHost(status_code=500)

    result = _import(cards, host)

    assert result["ok"] is False
    assert "500" in result["reason"]
    assert cards.list() == []


def test_import_is_inert_when_no_provider_is_configured(cards, host, monkeypatch):
    unconfigured = Settings()
    monkeypatch.setattr(issue_import, "get_settings", lambda: unconfigured)

    result = _import(cards, host)

    assert result["ok"] is True and result["imported"] == 0
    assert host.requests == []


def test_import_without_a_configured_project_says_so(cards, host, monkeypatch):
    """The reason now points at the tenant's Settings panel, not at an env var.

    RFC-0020 §3.3 retired ``CFACTORY_GITHUB_REPO`` as the place a project is
    named, so telling a user to set it would send them somewhere they cannot
    reach from the portal.
    """
    monkeypatch.setattr(issue_import, "get_settings", lambda: Settings(github_token=_TEST_TOKEN))

    result = _import(cards, host)

    assert result["ok"] is False
    assert "no project is configured for this tenant" in result["reason"]


# ── Safety: an imported card is NEVER ready ─────────────────────────────────


def test_an_imported_card_is_never_ready_even_when_the_issue_is_tiered(cards):
    """THE safety property (RFC-0020 §3.6).

    ``ready`` + a tier is the dispatch trigger, and real repositories are full of
    issues already labelled ``factory:low`` — so an importer able to produce
    ``ready`` is an importer that fires a build per issue from one click.
    """
    host = FakeHost(
        [
            _issue(1, labels=[{"name": "factory:low"}]),
            _issue(2, labels=[{"name": "factory:hard"}, {"name": "bug"}]),
            _issue(3, state="closed", labels=[{"name": "factory:medium"}]),
        ]
    )

    _import(cards, host)

    imported = {c.card_key: c for c in cards.list()}.values()
    assert all(c.status != "ready" for c in imported), "import must never dispatch"
    by_title = {c.title: c for c in imported}
    assert by_title["Issue 1"].status == "backlog"
    assert by_title["Issue 3"].status == "done"
    # The tiers ARE imported — they are just not enough to dispatch on their own.
    assert {c.tier for c in imported} == {"low", "hard", "medium"}


def test_importing_a_tiered_issue_dispatches_nothing(client, monkeypatch):
    """The end-to-end version: the intake hook must not fire on an import.

    Asserted at the dispatch seam rather than by inspecting statuses, because
    "no card is ready" and "nothing was dispatched" are different claims and this
    is the one that costs money.
    """
    dispatched: list[str] = []
    monkeypatch.setattr(
        card_ops, "maybe_dispatch", lambda _s, card, **_k: dispatched.append(card.card_key)
    )

    client.post("/api/cards/import")

    assert dispatched == []


# ── Idempotency ─────────────────────────────────────────────────────────────


def test_re_running_the_import_does_not_duplicate(cards, host):
    first = _import(cards, host)
    second = _import(cards, host, full=True)

    assert first["imported"] == 2
    assert second["imported"] == 0, "the second pass must adopt, not duplicate"
    assert len(cards.list()) == 2


def test_re_import_after_a_local_edit_mirrors_without_duplicating(cards, host):
    """The re-import that is most likely to go wrong: cards edited in between.

    The planner has moved a card, reprioritised it and renamed it. The host wins
    on the mirrored fields (title), the board keeps the planning-only ones
    (priority), and above all there is still ONE card per issue.
    """
    _import(cards, host)
    card = next(c for c in cards.list() if c.title == "Issue 1")
    cards.update(card.card_key, {"title": "Renamed locally", "priority": 3, "status": "blocked"})

    result = _import(cards, host, full=True)

    assert result["updated"] == 2 and result["imported"] == 0
    assert len(cards.list()) == 2
    refreshed = cards.get(card.card_key)
    assert refreshed.title == "Issue 1", "mirrored: the host wins on the title"
    assert refreshed.priority == 3, "planning-only: the board keeps the priority"
    assert refreshed.status == "backlog", "mirrored: the open issue is back in the backlog"


def test_one_issue_one_card_is_enforced_by_the_database(cards):
    """The guard itself, asserted directly rather than through the importer.

    RFC-0020 §3.6 puts idempotency in a UNIQUE ``(tenant_id, issue_ref)`` index
    precisely because an application-level check cannot survive two concurrent
    polls. A race test can pass by luck when the threads happen not to interleave;
    this one cannot — it fails the moment the constraint stops existing.
    """
    cards.create(CardCreate(title="First", issue_ref=f"{_REPO}#42"))

    with pytest.raises(DuplicateIssueRefError):
        cards.create(CardCreate(title="Second, same issue", issue_ref=f"{_REPO}#42"))

    # …and the same guard binds an UPDATE, not only an insert.
    other = cards.create(CardCreate(title="Unrelated"))
    with pytest.raises(DuplicateIssueRefError):
        cards.update(other.card_key, {"issue_ref": f"{_REPO}#42"})


def test_two_concurrent_imports_produce_one_card_each(cards, host):
    """The race an application-level existence check loses.

    Both passes see "no card for this issue" and both insert; the UNIQUE
    (tenant_id, issue_ref) index is what turns the loser's insert into an update.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(_import, cards, host) for _ in range(2)]
        tallies = [r.result() for r in results]

    assert all(t["ok"] for t in tallies)
    assert len(cards.list()) == 2, "a concurrent import must not duplicate"
    assert sum(t["imported"] for t in tallies) == 2


# ── Interaction with the stage actions (RFC-0020 §3.7) ──────────────────────


def test_an_imported_card_claims_no_factory_work(cards):
    """Importing records ISSUES; it must not claim work was ever started.

    `correlation_key` and `stage_runs` are the board's record of what the factory
    was asked to do. An imported card has neither until somebody dispatches it —
    which is also what keeps the §3.7 preconditions meaningful for it.
    """
    host = FakeHost([_issue(1, labels=[{"name": "factory:low"}])])

    _import(cards, host)

    card = cards.list()[0]
    assert card.correlation_key is None
    assert card.stage_runs == {}


def test_a_freshly_imported_card_cannot_be_tested(cards, ctx):
    """The precondition that proves the two features compose: a card with a tier
    but no build refuses `test` with `no_build_to_verify`, rather than generating
    verification lanes against nothing."""
    host = FakeHost([_issue(1, labels=[{"name": "factory:low"}])])
    _import(cards, host)
    card = cards.list()[0]

    with pytest.raises(StageRefusedError) as refusal:
        card_ops.run_card_stage(cards, ctx, card.card_key, "test", transport=host.transport())

    assert refusal.value.code == "no_build_to_verify"


def test_importing_never_makes_a_card_dispatchable_on_its_own(cards):
    """An imported card must not become dispatchable merely by existing.

    Asserted at the intake hook itself, on tiered issues — the dangerous case.
    `maybe_dispatch` returning None for every imported card is the statement that
    matters: no board write, no poll, and no later refactor that runs the hook
    over the backlog can turn one import into a hundred builds.
    """
    host = FakeHost(
        [_issue(1, labels=[{"name": "factory:low"}]), _issue(2, labels=[{"name": "factory:hard"}])]
    )

    _import(cards, host)

    imported = cards.list()
    assert len(imported) == 2, "the fixture must actually have imported something"
    for card in imported:
        assert card.status == "backlog"
        assert card.tier is not None, "…and these are exactly the tiered, dispatchable-looking ones"
        assert maybe_dispatch(cards, card, transport=host.transport()) is None


# ── Mapping ─────────────────────────────────────────────────────────────────


def test_issue_fields_map_onto_the_card(cards):
    host = FakeHost(
        [
            _issue(
                7,
                title="Widget throughput",
                body="The widgets are slow.",
                labels=[{"name": "factory:medium"}, {"name": "perf"}],
                assignees=[{"login": "ada"}, {"login": "grace"}],
                milestone={"title": "v2"},
            )
        ]
    )

    _import(cards, host)

    card = cards.list()[0]
    assert card.title == "Widget throughput"
    assert card.description == "The widgets are slow."
    assert card.tier == "medium"
    assert card.labels == ["factory:medium", "perf"]
    assert card.assignee == "ada", "assignees[0], not the whole list"
    assert card.milestone == "v2"
    assert card.issue_state == "open"
    assert card.issue_ref == f"{_REPO}#7", "the ref is preserved so later sync works"
    assert card.priority == issue_import.IMPORT_PRIORITY


def test_the_body_never_becomes_acceptance_criteria(cards):
    """Parsing prose into testable statements would FABRICATE what the factory
    verifies against (RFC-0020 §3.6). Import leaves the list empty."""
    host = FakeHost([_issue(1, body="- [ ] it must be fast\n- [ ] it must not crash")])

    _import(cards, host)

    card = cards.list()[0]
    assert card.acceptance_criteria == []
    assert "must be fast" in card.description


def test_an_unknown_factory_label_is_not_guessed_at_as_a_tier(cards):
    host = FakeHost([_issue(1, labels=[{"name": "factory:urgent"}])])

    _import(cards, host)

    assert cards.list()[0].tier is None


# ── Ongoing reconciliation (poll-based, NOT live) ───────────────────────────


def test_the_watermark_is_the_newest_update_minus_an_overlap(cards, host):
    _import(cards, host)

    watermark = cards.get_watermark(_REPO)
    expected = datetime(2026, 7, 20, 10, 0, tzinfo=UTC) - timedelta(seconds=60)
    assert watermark == expected


def test_the_second_pass_is_incremental_and_asks_for_every_state(cards, host):
    """Closures and reopenings are exactly what an ``open``-only poll would miss,
    so the incremental pass widens the state rather than narrowing it."""
    _import(cards, host)
    result = _import(cards, host)

    assert result["incremental"] is True
    params = host.list_calls[-1].url.params
    assert params.get("state") == "all"
    assert params.get("since") == "2026-07-20T09:59:00Z"


def test_an_issue_filed_after_the_first_pass_appears_on_the_next(cards, host):
    _import(cards, host)
    host.issues = [_issue(3, title="Filed later", updated_at="2026-07-21T10:00:00Z")]

    result = _import(cards, host)

    assert result["imported"] == 1
    assert "Filed later" in {c.title for c in cards.list()}


def test_import_reports_that_it_is_not_live(cards, host):
    """Freshness is stated, never implied: there is no webhook receiver."""
    result = _import(cards, host)

    assert result["live"] is False
    assert result["last_synced_at"]


# ── Closure, deletion, disappearance ────────────────────────────────────────


def test_an_issue_closed_upstream_moves_its_card_to_done(cards, host):
    _import(cards, host)
    host.issues = [_issue(1, state="closed"), _issue(2)]

    _import(cards, host, full=True)

    card = next(c for c in cards.list() if c.title == "Issue 1")
    assert card.status == "done"
    assert card.issue_state == "closed"


def test_a_reopened_issue_comes_back_to_the_backlog(cards, host):
    host.issues = [_issue(1, state="closed")]
    _import(cards, host)
    host.issues = [_issue(1, state="open")]

    _import(cards, host, full=True)

    assert cards.list()[0].status == "backlog"


def test_a_card_in_flight_keeps_its_status(cards, host):
    """A run in flight owns its status: a poll may not stomp it back to backlog
    because somebody reopened the issue (RFC-0020 §3.6)."""
    _import(cards, host)
    card = next(c for c in cards.list() if c.title == "Issue 1")
    cards.update(card.card_key, {"correlation_key": "corr-1", "status": "in_progress"})
    host.issues = [_issue(1, state="closed", title="Renamed upstream"), _issue(2)]

    _import(cards, host, full=True)

    refreshed = cards.get(card.card_key)
    assert refreshed.status == "in_progress", "the run owns the status"
    assert refreshed.issue_state == "closed", "…but the issue's state is still mirrored"
    assert refreshed.title == "Renamed upstream"


def test_a_failed_dispatch_is_not_un_blocked_by_the_next_poll(cards, host):
    """The RFC-0020 §3.7 interaction: `correlation_key` alone is not "in the factory".

    A dispatch that FAILED leaves a `stage_runs` record and a `blocked` card with
    no correlation key at all — §3.7 writes the key only when a stage actually
    lands. A poll that read only the key would cheerfully move that card back to
    `backlog` and erase the fact that a build was attempted and broke.
    """
    _import(cards, host)
    card = next(c for c in cards.list() if c.title == "Issue 1")
    cards.update(
        card.card_key,
        {
            "status": "blocked",
            "stage_runs": {"code": {"service": "aifactory", "status": "failed"}},
        },
    )

    _import(cards, host, full=True)

    refreshed = cards.get(card.card_key)
    assert refreshed.correlation_key is None, "a failed dispatch never got a key"
    assert refreshed.status == "blocked", "the failed stage still owns the column"


def test_a_deleted_card_is_not_resurrected_by_the_next_import(cards, host):
    """Deleting a card means "not on my board"; the issue is untouched and the
    next poll does NOT bring the card back."""
    _import(cards, host)
    card = next(c for c in cards.list() if c.title == "Issue 1")

    assert cards.delete(card.card_key) is True

    result = _import(cards, host, full=True)

    assert result["skipped"] == 1
    assert [c.title for c in cards.list()] == ["Issue 2"]
    assert cards.get(card.card_key) is None


def test_deleting_a_card_never_touches_the_issue(cards, host):
    _import(cards, host)
    card = cards.list()[0]
    before = len(host.requests)

    cards.delete(card.card_key)

    assert len(host.requests) == before, "a local delete makes no call to the host"


def test_a_404_marks_the_card_missing_rather_than_deleting_it(cards):
    """An issue deleted or transferred on the host (RFC-0020 §3.6). Human
    planning data is not destroyed by a 404."""
    card = cards.create(CardCreate(title="Widget throughput", issue_ref=f"{_REPO}#42"))
    gone = FakeHost(status_code=404)

    result = github_sync.sync_card(cards, card, transport=gone.transport())

    assert result["ok"] is False
    refreshed = cards.get(card.card_key)
    assert refreshed is not None, "the card survives"
    assert refreshed.issue_state == "missing"


def test_a_transient_failure_is_not_read_as_a_missing_issue(cards):
    """Only a 404 means "gone". A 403 or a timeout means "we could not read it",
    which is a stale card, not a deleted issue."""
    card = cards.create(CardCreate(title="Widget throughput", issue_ref=f"{_REPO}#42"))
    rate_limited = FakeHost(status_code=403)

    github_sync.sync_card(cards, card, transport=rate_limited.transport())

    assert cards.get(card.card_key).issue_state != "missing"


# ── Surfaces: REST + MCP parity, audited ────────────────────────────────────


def test_import_over_rest(client, cards):
    body = client.post("/api/cards/import").json()

    assert body["ok"] is True and body["imported"] == 2
    assert len(cards.list()) == 2


def test_import_over_mcp(client, cards):
    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_import_cards", "arguments": {}},
        },
    ).json()

    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["ok"] is True and payload["imported"] == 2
    assert len(cards.list()) == 2


def test_rest_and_mcp_import_the_same_way(client, cards):
    """The two surfaces are one operation: the second call updates the cards the
    first created, whichever transport made it."""
    rest = client.post("/api/cards/import?full=true").json()
    mcp_body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_import_cards", "arguments": {"full": True}},
        },
    ).json()
    over_mcp = json.loads(mcp_body["result"]["content"][0]["text"])

    assert rest["imported"] == 2 and over_mcp["imported"] == 0
    assert over_mcp["updated"] == 2
    assert len(cards.list()) == 2


def test_import_is_audited_over_both_surfaces(client, audit):
    client.post("/api/cards/import")
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_import_cards", "arguments": {}},
        },
    )

    kinds = [e.kind for e in audit.list()]
    assert kinds.count("import_cards") == 2


def test_import_requires_the_write_scope(client):
    auth.set_keys({"reader": {"read"}})

    resp = client.post("/api/cards/import", headers={"Authorization": "Bearer reader"})

    assert resp.status_code == 403


# ── Provider-agnostic: the same import over GitLab ──────────────────────────


def _gitlab_issue(iid: int, **overrides) -> dict:
    """GitLab's issue shape: `iid` not `number`, `description` not `body`,
    `opened` not `open`, and labels as bare strings."""
    return {
        "id": 9000 + iid,
        "iid": iid,
        "title": f"Issue {iid}",
        "description": f"Body of {iid}",
        "state": "opened",
        "labels": ["factory:low"],
        "assignees": [{"username": "ada"}],
        "milestone": {"title": "v2"},
        "author": {"username": "someone"},
        "created_at": _UPDATED,
        "updated_at": _UPDATED,
        "web_url": f"https://gitlab.com/acme/widgets/-/issues/{iid}",
        **overrides,
    }


def test_import_works_on_gitlab_through_the_same_protocol(cards, monkeypatch):
    """The import is not a GitHub feature wearing a protocol.

    Same importer, same assertions, a completely different host shape — normalised
    by the CANONICAL GitLab provider, which this code has never seen the inside of.

    Its client is patched rather than handed a transport, as in
    ``test_git_providers.py``: the vendored provider builds its own
    ``AsyncClient`` and vendored code is not edited to add a seam.
    """
    gitlab = Settings(
        git_provider="gitlab",
        git_provider_token=_TEST_TOKEN,
        git_provider_url="https://gitlab.test",
        github_repo="acme/widgets",
    )
    monkeypatch.setattr(issue_import, "get_settings", lambda: gitlab)
    monkeypatch.setattr(github_sync, "get_settings", lambda: gitlab)
    host = FakeHost([_gitlab_issue(11), _gitlab_issue(12, state="closed")])
    monkeypatch.setattr(
        GitLabProvider,
        "_client",
        lambda self: httpx.AsyncClient(
            base_url="https://gitlab.test",
            headers=self._headers,
            transport=host.transport(),
        ),
    )

    result = _import(cards, host)

    assert result["ok"] is True and result["imported"] == 2
    by_title = {c.title: c for c in cards.list()}
    open_card = by_title["Issue 11"]
    assert open_card.status == "backlog", "still never ready, on any host"
    assert open_card.issue_state == "open", "GitLab's 'opened' arrives normalised"
    assert open_card.issue_ref == "acme/widgets#11", "the IID, not the global id"
    assert open_card.tier == "low"
    assert by_title["Issue 12"].status == "done"

    # And it is still idempotent on a host whose payloads look nothing alike.
    assert _import(cards, host, full=True)["imported"] == 0
    assert len(cards.list()) == 2

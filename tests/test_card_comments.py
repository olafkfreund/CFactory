"""Imported issue comments on cards (Factory#375).

The body was only half the issue. "What about the details and the comments we
need those as well" — for planning, the thread is usually where the decision
lives, and a card that dropped it dropped the decision.

Comments are STORED, not fetched on card open, and refreshed by the SAME
incremental pass that refreshes the issue (#374). That is one sync path, not two.

Four properties carry weight here, and each has a test named after it:

* **tenant isolation** — a comment is read through the same tenant scope as its
  card. Drop the scope and
  ``test_one_tenant_never_reads_another_tenants_comments`` fails. (Mutation check
  a.)
* **a failed fetch is never stored as an empty thread** — an issue with no
  discussion and an issue whose discussion failed to download must stay
  distinguishable, and the only thing distinguishing them is
  ``comments_synced_at``. Persist the empty list as complete and
  ``test_a_failed_comment_fetch_never_claims_the_thread_is_empty`` fails.
  (Mutation check b.)
* **re-import updates, never duplicates** — idempotent on the PROVIDER's comment
  id, enforced by the unique index rather than by a lookup.
* **multi-connection** — a card on connection A and a card on connection B read
  their comments from DIFFERENT hosts, resolved through each card's own
  repository. #216 fixed exactly this for issue links; it must not come back for
  comments.

Every test drives the real provider path through an ``httpx.MockTransport``, so
what is asserted is the requests a host would actually see.
"""

from __future__ import annotations

import re

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cfactory import auth, card_ops, config, github_sync, issue_import, mcp
from cfactory import cards as cards_module
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardComment, CardCreate, CardStore
from cfactory.config import Settings
from cfactory.git_connections import GitConnectionCreate, GitRepositoryCreate
from cfactory.issue_import import import_issues
from fastapi.testclient import TestClient

#: A client-safe failure reason: a correlation id, and nothing that names
#: an internal host, path or library (CWE-209).
_REFERENCE_RE = re.compile(r"reference ([0-9a-f]{12})")


# Fake key material, pinned so a failure is reproducible. Not a secret: it
# protects nothing but this module's temp databases.
_KEY = f"v1:{base64.b64encode(b'c9' * 16).decode()}"

_A_TOKEN = "tenant-a-comment-credential-1a2b"  # noqa: S105 — a fake, not a secret
_B_TOKEN = "tenant-b-comment-credential-3c4d"  # noqa: S105 — a fake, not a secret
_WRITER = "comment-test-writer-key"
_HMAC = "card-comments-test-hmac"

_PROJECT = "acme/widgets"
_OTHER_PROJECT = "acme/gadgets"

_HTTP_SERVER_ERROR = 503
_HTTP_NOT_FOUND = 404

_TWO = 2


def _issue(number: int, *, updated: str = "2026-07-20T10:00:00Z") -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": f"Body of {number}",
        "state": "open",
        "labels": [],
        "assignees": [],
        "milestone": None,
        "user": {"login": "someone"},
        "created_at": "2026-07-20T09:00:00Z",
        "updated_at": updated,
        "html_url": f"https://example.test/{number}",
    }


def _comment(
    comment_id: int,
    issue_number: int,
    *,
    body: str = "the decision lives here",
    author: str = "reviewer",
    project: str = _PROJECT,
    created: str = "2026-07-20T11:00:00Z",
) -> dict:
    """One GitHub issue-comment payload, as both comment endpoints return it."""
    return {
        "id": comment_id,
        "user": {"login": author},
        "body": body,
        "created_at": created,
        "updated_at": created,
        "html_url": f"https://example.test/{issue_number}#issuecomment-{comment_id}",
        # The repository-wide endpoint carries no issue number; this URL is the
        # only link back, which is why the provider parses it.
        "issue_url": f"https://api.github.com/repos/{project}/issues/{issue_number}",
    }


class Host:
    """A stand-in git host serving issues AND their comments, per project."""

    def __init__(self) -> None:
        self.issues: dict[str, list[dict]] = {}
        self.comments: dict[str, list[dict]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_comments: set[str] = set()

    def for_project(self, project: str) -> list[dict]:
        return self.issues.setdefault(project, [])

    def comments_for(self, project: str) -> list[dict]:
        return self.comments.setdefault(project, [])

    def _project_of(self, request: httpx.Request) -> str:
        parts = request.url.path.strip("/").split("/")
        return "/".join(parts[1:3]) if len(parts) >= _TWO + 1 else ""

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        project = self._project_of(request)
        if int(request.url.params.get("page", "1")) > 1:
            return httpx.Response(200, json=[])
        if "/comments" in request.url.path:
            if project in self.fail_comments:
                return httpx.Response(_HTTP_SERVER_ERROR, json={"message": "comments are down"})
            return httpx.Response(200, json=self.comments_for(project))
        return httpx.Response(200, json=self.for_project(project))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @property
    def comment_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/comments" in r.url.path]

    @property
    def comment_hosts(self) -> set[str]:
        return {r.url.host for r in self.comment_calls}


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_HMAC)


@pytest.fixture
def settings():
    return Settings(
        github_token=_A_TOKEN,
        github_repo=_PROJECT,
        github_api_url="https://gh.test",
        credential_key=_KEY,
    )


@pytest.fixture(autouse=True)
def _configured(monkeypatch, settings):
    for module in (cards_module, github_sync, issue_import, card_ops):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def host():
    return Host()


@pytest.fixture
def client(cards, audit, host, monkeypatch):
    """One TestClient over both surfaces, one store, one fake host."""
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", host.transport)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    auth.set_keys({_WRITER: {"read", "write"}})

    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = host.transport
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.set_keys({})


def _repositories(store: CardStore, projects: list[str], *, token: str) -> list[int]:
    connection = store.create_connection(GitConnectionCreate(provider="github"))
    store.set_connection_credential(connection.id, token)
    return [
        store.create_repository(connection.id, GitRepositoryCreate(project=p)).id for p in projects
    ]


def _import(store: CardStore, host_: Host, settings_: Settings, **kwargs):
    return import_issues(store, settings=settings_, transport=host_.transport(), **kwargs)


# ── the acceptance criterion ─────────────────────────────────────────────────


def test_importing_an_issue_brings_its_discussion_with_it(cards, host, settings):
    """Factory#375's acceptance: the thread arrives with the card, not on a
    separate mechanism somebody has to trigger."""
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).extend(
        [
            _comment(101, 1, body="first thought", created="2026-07-20T11:00:00Z"),
            _comment(102, 1, body="**the** decision", created="2026-07-20T12:00:00Z"),
        ]
    )

    result = _import(cards, host, settings)

    assert result["ok"] is True
    assert result["comments"]["ok"] is True
    card = cards.list()[0]
    assert card.comment_count == _TWO
    assert card.comments_synced_at is not None
    bodies = [c.body for c in cards.comments(card.card_key)]
    assert bodies == ["first thought", "**the** decision"]  # oldest first


def test_an_issue_with_no_discussion_is_recorded_as_having_none(cards, host, settings):
    """The other half of the honesty rule: zero comments AND a timestamp means
    "no discussion", which is a real answer and must be storable."""
    host.for_project(_PROJECT).append(_issue(1))

    _import(cards, host, settings)

    card = cards.list()[0]
    assert card.comment_count == 0
    assert card.comments_synced_at is not None  # read successfully; there is none


# ── MUTATION CHECK (b): a failed fetch never becomes an empty thread ─────────


def test_a_failed_comment_fetch_never_claims_the_thread_is_empty(cards, host, settings):
    """MUTATION CHECK (b). Make a failed fetch persist an empty list as if it were
    complete — store ``[]`` and stamp ``comments_synced_at`` when the read raised
    — and this fails.

    An issue with no discussion and an issue whose discussion failed to download
    are the same zero rows. The ONLY thing that tells them apart is the marker,
    so a failure must leave it NULL. Get this wrong and the board reports "no
    discussion" for an issue whose comments merely did not arrive, which is
    precisely the data loss Factory#375 reports.
    """
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1))
    host.fail_comments.add(_PROJECT)

    result = _import(cards, host, settings)

    # The ISSUE import still succeeded — a comment outage must not fail the board.
    assert result["ok"] is True
    assert result["imported"] == 1
    # And the comment half reported its failure rather than swallowing it.
    assert result["comments"]["ok"] is False
    assert _REFERENCE_RE.search(result["comments"]["reason"])

    card = cards.list()[0]
    assert card.comments_synced_at is None, "a failed fetch claimed the thread was complete"
    assert card.comment_count == 0
    assert cards.comments(card.card_key) == []


def test_a_recovered_provider_backfills_the_thread_it_could_not_read(cards, host, settings):
    """The marker is not a tombstone: the next pass retries the card it failed."""
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1))
    host.fail_comments.add(_PROJECT)
    _import(cards, host, settings)
    assert cards.list()[0].comments_synced_at is None

    host.fail_comments.clear()
    _import(cards, host, settings)

    card = cards.list()[0]
    assert card.comments_synced_at is not None
    assert [c.body for c in cards.comments(card.card_key)] == ["the decision lives here"]


def test_a_failed_refresh_keeps_the_copy_it_already_had(cards, host, settings):
    """A later outage must not blank a thread that was read successfully before.

    The stored copy surviving an outage is half the reason storing beat fetching
    on open in the first place.
    """
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1, body="kept"))
    _import(cards, host, settings)
    synced = cards.list()[0].comments_synced_at
    assert synced is not None

    host.fail_comments.add(_PROJECT)
    result = _import(cards, host, settings)

    assert result["comments"]["ok"] is False
    card = cards.list()[0]
    assert [c.body for c in cards.comments(card.card_key)] == ["kept"]
    # The marker did not move: what is stored is still complete as of the LAST
    # successful read, and saying so is the honest thing.
    assert card.comments_synced_at == synced


# ── MUTATION CHECK (a): tenant isolation ─────────────────────────────────────


def test_one_tenant_never_reads_another_tenants_comments(tmp_path, host, settings):
    """MUTATION CHECK (a). Break the tenant scope on comments — read them without
    the store's tenant filter — and tenant B's discussion is served to tenant A.

    Two tenants, same database, and (deliberately) the SAME card key in both, so
    an unscoped read returns something rather than nothing and the test cannot
    pass by accident.
    """
    shared = CardStore(f"sqlite:///{tmp_path / 'shared.db'}")
    a = shared.scoped("tenant-a")
    b = shared.scoped("tenant-b")
    now = datetime.now(UTC)
    a.create(CardCreate(card_key="FCT-1", title="A's card", issue_ref=f"{_PROJECT}#1"))
    b.create(CardCreate(card_key="FCT-1", title="B's card", issue_ref=f"{_OTHER_PROJECT}#1"))
    b.store_comments(
        "FCT-1",
        [
            CardComment(
                comment_id="9",
                author="b-person",
                body="tenant B's confidential discussion",
                url="",
                created_at=now,
                updated_at=now,
            )
        ],
        synced_at=now,
    )

    assert a.comments("FCT-1") == []
    assert a.get("FCT-1").comment_count == 0
    assert a.get("FCT-1").comments_synced_at is None
    # ... and B still has its own.
    assert [c.body for c in b.comments("FCT-1")] == ["tenant B's confidential discussion"]
    assert b.get("FCT-1").comment_count == 1


def test_the_comments_endpoint_is_scoped_to_the_callers_tenant(tmp_path, host, settings):
    """The same guard at the API edge, where a card key from elsewhere is not a
    capability: it is a 404, not somebody else's thread."""
    shared = CardStore(f"sqlite:///{tmp_path / 'shared.db'}")
    a = shared.scoped("tenant-a")
    b = shared.scoped("tenant-b")
    now = datetime.now(UTC)
    b.create(CardCreate(card_key="FCT-99", title="B's card"))
    b.store_comments(
        "FCT-99",
        [
            CardComment(
                comment_id="9", author="b", body="B only", url="", created_at=now, updated_at=now
            )
        ],
        synced_at=now,
    )

    auth.set_keys({_WRITER: {"read", "write"}})
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: a
    client = TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    try:
        resp = client.get("/api/cards/FCT-99/comments")
    finally:
        auth.set_keys({})

    assert resp.status_code == _HTTP_NOT_FOUND
    assert "B only" not in resp.text


# ── idempotency: re-import updates, never duplicates ─────────────────────────


def test_re_importing_updates_a_comment_and_never_duplicates_it(cards, host, settings):
    """Idempotent on the PROVIDER's comment id, exactly as the card import is
    idempotent on ``issue_ref``. Somebody edits a comment; the board must hold one
    row saying the new thing, not two rows disagreeing."""
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1, body="original"))
    _import(cards, host, settings)
    card_key = cards.list()[0].card_key

    host.comments[_PROJECT] = [_comment(101, 1, body="edited after review")]
    _import(cards, host, settings, full=True)

    stored = cards.comments(card_key)
    assert len(stored) == 1
    assert stored[0].comment_id == "101"
    assert stored[0].body == "edited after review"
    assert cards.get(card_key).comment_count == 1


def test_the_unique_index_is_the_guard_not_the_lookup(cards, settings):
    """Store the same provider comment id twice through the store directly: the
    database constraint refuses a second row even with no read in between."""
    now = datetime.now(UTC)
    cards.create(CardCreate(card_key="FCT-1", title="t"))
    twice = [
        CardComment(
            comment_id="7", author="x", body=body, url="", created_at=now, updated_at=now
        )
        for body in ("first", "second")
    ]

    for comment in twice:
        cards.store_comments("FCT-1", [comment], synced_at=now)

    stored = cards.comments("FCT-1")
    assert len(stored) == 1
    assert stored[0].body == "second"


# ── multi-connection: the CARD's repository decides the host (#373, #216) ────


def test_cards_on_two_connections_fetch_comments_from_different_hosts(
    tmp_path, host, settings, monkeypatch
):
    """A card on connection A and a card on connection B read their comments from
    THEIR OWN hosts.

    #216 fixed exactly this for issue links — a card imported from one connection
    must not be resolved against the tenant's default connection — and the bug
    must not come back wearing comments. Two GitHub connections on two base URLs,
    two projects, and the assertion is on the HOSTS the comment reads went to.
    """
    store = CardStore(f"sqlite:///{tmp_path / 'multi.db'}")
    first = store.create_connection(
        GitConnectionCreate(provider="github", base_url="https://one.test")
    )
    store.set_connection_credential(first.id, _A_TOKEN)
    repo_one = store.create_repository(first.id, GitRepositoryCreate(project=_PROJECT))
    second = store.create_connection(
        GitConnectionCreate(provider="github", base_url="https://two.test")
    )
    store.set_connection_credential(second.id, _B_TOKEN)
    repo_two = store.create_repository(second.id, GitRepositoryCreate(project=_OTHER_PROJECT))

    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1, body="on one.test"))
    host.for_project(_OTHER_PROJECT).append(_issue(2))
    host.comments_for(_OTHER_PROJECT).append(
        _comment(202, 2, body="on two.test", project=_OTHER_PROJECT)
    )

    import_issues(store, settings=settings, repository_id=repo_one.id, transport=host.transport())
    import_issues(store, settings=settings, repository_id=repo_two.id, transport=host.transport())

    assert host.comment_hosts == {"one.test", "two.test"}
    by_ref = {c.issue_ref: c for c in store.list()}
    one = store.comments(by_ref[f"{_PROJECT}#1"].card_key)
    two = store.comments(by_ref[f"{_OTHER_PROJECT}#2"].card_key)
    assert [c.body for c in one] == ["on one.test"]
    assert [c.body for c in two] == ["on two.test"]
    # The credentials did not cross either: each host saw only its own.
    for request in host.comment_calls:
        expected = _A_TOKEN if request.url.host == "one.test" else _B_TOKEN
        assert request.headers["authorization"] == f"Bearer {expected}"


def test_one_connections_comments_never_land_on_another_connections_card(
    tmp_path, host, settings
):
    """The narrower half: a comment is only ever stored against a card in the
    project it was read from, so a shared issue NUMBER cannot cross repositories.

    Both projects have an issue #1. Without project scoping, one bulk read would
    happily attach its comments to the other repository's card.
    """
    store = CardStore(f"sqlite:///{tmp_path / 'cross.db'}")
    connection = store.create_connection(GitConnectionCreate(provider="github"))
    store.set_connection_credential(connection.id, _A_TOKEN)
    repo_one = store.create_repository(connection.id, GitRepositoryCreate(project=_PROJECT))
    store.create_repository(connection.id, GitRepositoryCreate(project=_OTHER_PROJECT))

    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1, body="belongs to widgets"))
    host.for_project(_OTHER_PROJECT).append(_issue(1))
    host.comments_for(_OTHER_PROJECT).append(
        _comment(999, 1, body="belongs to gadgets", project=_OTHER_PROJECT)
    )

    import_issues(store, settings=settings, repository_id=repo_one.id, transport=host.transport())

    by_ref = {c.issue_ref: c for c in store.list()}
    assert [c.body for c in store.comments(by_ref[f"{_PROJECT}#1"].card_key)] == [
        "belongs to widgets"
    ]
    # The gadgets card was never imported by this pass and holds nothing.
    assert f"{_OTHER_PROJECT}#1" not in by_ref


# ── cost: the bulk path, and the bounded backfill ────────────────────────────


def test_a_cold_backfill_is_bounded_rather_than_fired_in_one_tick(cards, host, settings):
    """A freshly connected repository must not spend one comment call per issue in
    a single tick — that is the stampede the poll's pacing exists to prevent.

    The bound is ``CFACTORY_IMPORT_COMMENT_BACKFILL_MAX``; the rest arrive on later
    passes, and no card is ever marked complete before it has actually been read.
    """
    settings.import_comment_backfill_max = 2
    host.for_project(_PROJECT).extend(_issue(n) for n in range(1, 6))

    _import(cards, host, settings)

    assert len(host.comment_calls) == _TWO
    synced = [c for c in cards.list() if c.comments_synced_at is not None]
    assert len(synced) == _TWO
    # The three not reached say "unknown", not "no discussion".
    assert len([c for c in cards.list() if c.comments_synced_at is None]) == 3


def test_the_refresh_uses_one_bulk_call_for_the_whole_board(cards, host, settings):
    """THE affordability property. Once every card has a complete copy, refreshing
    all of them is ONE request with a server-side ``since`` — the repository-wide
    comments endpoint — not one per card.
    """
    host.for_project(_PROJECT).extend(_issue(n) for n in range(1, 6))
    _import(cards, host, settings)  # backfill: one call per card
    assert all(c.comments_synced_at is not None for c in cards.list())
    host.requests.clear()

    _import(cards, host, settings)

    assert len(host.comment_calls) == 1
    call = host.comment_calls[0]
    assert call.url.path.endswith("/issues/comments")  # the bulk endpoint
    assert call.url.params.get("since")  # narrowed server-side


def test_the_refresh_window_is_the_oldest_copy_not_the_newest(cards, host, settings):
    """One request covers cards synced at different times, so the window has to be
    the OLDEST card's — asking from the newest would leave the older card a hole.
    """
    host.for_project(_PROJECT).extend([_issue(1), _issue(2)])
    settings.import_comment_backfill_max = 1
    _import(cards, host, settings)  # card 1 only
    older = min(c.comments_synced_at for c in cards.list() if c.comments_synced_at)
    settings.import_comment_backfill_max = 25
    _import(cards, host, settings)  # card 2 backfilled, card 1 refreshed
    host.requests.clear()

    _import(cards, host, settings)

    since = datetime.fromisoformat(
        host.comment_calls[0].url.params["since"].replace("Z", "+00:00")
    )
    oldest = older if older.tzinfo else older.replace(tzinfo=UTC)
    assert since <= oldest
    # ... and rewound by the skew overlap rather than sitting exactly on it.
    assert oldest - since >= timedelta(minutes=1)


def test_comment_import_can_be_switched_off(cards, host, settings):
    """``CFACTORY_IMPORT_COMMENTS=false``: issues still import, no comment call is
    made, and no card claims a thread it never read."""
    settings.import_comments = False
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1))

    result = _import(cards, host, settings)

    assert result["imported"] == 1
    assert host.comment_calls == []
    assert cards.list()[0].comments_synced_at is None


def test_a_deleted_card_stops_costing_comment_calls(cards, host, settings):
    """A card taken off the board must not keep spending a provider call. The
    tombstone survives (so the import does not resurrect it) but it is not read."""
    host.for_project(_PROJECT).extend([_issue(1), _issue(2)])
    _import(cards, host, settings)
    cards.delete(cards.list()[0].card_key)
    host.requests.clear()

    result = _import(cards, host, settings)

    assert result["comments"]["cards"] == 1


# ── the API and MCP surfaces ─────────────────────────────────────────────────


def test_the_card_api_carries_the_count_and_the_thread_is_its_own_read(
    client, cards, host, settings
):
    """The list stays small: a card carries two scalars, and the bodies are a
    separate GET. A 46-card board must not ship 46 comment threads."""
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1, body="see the thread"))
    _import(cards, host, settings)
    card_key = cards.list()[0].card_key

    listing = client.get("/api/cards").json()["cards"][0]
    assert listing["comment_count"] == 1
    assert listing["comments_synced_at"]
    assert "see the thread" not in str(listing)

    thread = client.get(f"/api/cards/{card_key}/comments").json()
    assert thread["count"] == 1
    assert thread["synced_at"]
    assert thread["comments"][0]["body"] == "see the thread"
    assert thread["comments"][0]["comment_id"] == "101"
    assert thread["comments"][0]["author"] == "reviewer"


def test_the_endpoint_distinguishes_no_discussion_from_never_read(client, cards):
    """The honesty rule, at the surface a client actually reads."""
    cards.create(CardCreate(card_key="FCT-1", title="never read"))
    now = datetime.now(UTC)
    cards.create(CardCreate(card_key="FCT-2", title="no discussion"))
    cards.store_comments("FCT-2", [], synced_at=now)

    unknown = client.get("/api/cards/FCT-1/comments").json()
    empty = client.get("/api/cards/FCT-2/comments").json()

    assert unknown["count"] == 0
    assert unknown["synced_at"] is None
    assert empty["count"] == 0
    assert empty["synced_at"] is not None


def test_the_mcp_tool_returns_the_same_thread_as_rest(client, cards, host, settings):
    """RFC-0019 §3.3 programmatic equivalence: one implementation, two surfaces."""
    host.for_project(_PROJECT).append(_issue(1))
    host.comments_for(_PROJECT).append(_comment(101, 1))
    _import(cards, host, settings)
    card_key = cards.list()[0].card_key

    rest = client.get(f"/api/cards/{card_key}/comments").json()
    mcp_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_card_comments", "arguments": {"card_key": card_key}},
        },
    ).json()

    payload = mcp_resp["result"]["content"][0]["text"]
    assert rest["comments"][0]["body"] in payload
    assert rest["synced_at"] in payload


def test_reading_comments_needs_only_the_read_scope(cards, host, settings):
    """A read-only key can see the discussion; it is text the host already shows
    anyone who can see the issue."""
    cards.create(CardCreate(card_key="FCT-1", title="t"))
    auth.set_keys({"reader": {"read"}})
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    try:
        resp = TestClient(app, headers={"Authorization": "Bearer reader"}).get(
            "/api/cards/FCT-1/comments"
        )
    finally:
        auth.set_keys({})

    assert resp.status_code == 200

"""The board reconciles itself — the periodic per-repository poll (#374).

``POST /api/cards/import`` was already incremental, idempotent and safe to repeat
(``test_issue_import.py`` covers all of that). What it had no test for was anything
CALLING it: the board drifted from the repository the moment somebody filed, closed
or edited an issue, and nothing in the UI said so. This module covers the loop that
now does the calling, and the staleness read that makes a drifted board visible.

Four guards are mutation-checked — weaken one and a named test here goes red:

* **tenant scoping** — the poll enumerates repositories through the tenant-scoped
  store and resolves each one's credential through the same scope. Poll from an
  unscoped store (or resolve a repository id the scope did not hand out) and
  ``test_the_poll_never_reads_one_tenants_repository_with_anothers_credential``
  fails;
* **the stampede bound** — one repository at a time, paced by
  ``CFACTORY_IMPORT_POLL_GAP_SECONDS``. Replace the sequential loop with a gather,
  or drop the gap, and
  ``test_a_tenant_with_many_repositories_does_not_stampede_the_provider`` fails;
* **backoff** — a failing repository sits out cycles instead of being asked again
  at full cadence, and a rate-limit refusal backs off harder than a plain error.
  Remove the backoff and ``test_a_failing_repository_is_not_polled_every_cycle``
  fails;
* **one poller per cycle** — the ``card_import_state`` lease, so a second replica
  waking at the same moment skips what the first is reading. Remove the claim and
  ``test_a_second_replica_skips_the_cycle_the_first_one_claimed`` fails.

Every test drives the REAL provider path through an ``httpx.MockTransport``, so
what is asserted is the requests a host would actually see.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cfactory import auth, card_ops, config, github_sync, issue_import, mcp
from cfactory import cards as cards_module
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from cfactory.config import Settings
from cfactory.git_config import GitConfigUpdate
from cfactory.git_connections import GitConnectionCreate, GitRepositoryCreate
from cfactory.issue_import import PollBackoff, import_issues, poll_once
from fastapi.testclient import TestClient
from runners.github.providers.protocol import IssueData, ProviderType

# Fake key material, pinned so a failure is reproducible. Not a secret: it protects
# nothing but this module's temp databases.
_KEY = f"v1:{base64.b64encode(b'p7' * 16).decode()}"

_A_TOKEN = "tenant-a-provider-credential-1a2b"  # noqa: S105 — a fake, not a secret
_B_TOKEN = "tenant-b-provider-credential-3c4d"  # noqa: S105 — a fake, not a secret
_WRITER = "poll-test-writer-key"
_HMAC = "issue-poll-test-hmac"

_A_PROJECT = "tenant-a/widgets"
_B_PROJECT = "tenant-b/gadgets"

# "/repos/{owner}/{repo}/..." — the shortest path a project can be read out of.
_PROJECT_PATH_PARTS = 3
_BACKOFF_CYCLE_GUARD = 10

_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 503

_STALE_CYCLES = card_ops.STALE_CYCLES


def _issue_data() -> IssueData:
    """One provider-neutral issue, for the mirrored-field assertion."""
    now = datetime.now(UTC)
    return IssueData(
        number=1,
        title="t",
        body="b",
        author="a",
        state="open",
        labels=[],
        created_at=now,
        updated_at=now,
        url="",
        assignees=[],
        milestone=None,
        provider=ProviderType.GITHUB,
        raw_data={},
    )


def _issue(number: int, *, state: str = "open", updated: str = "2026-07-20T10:00:00Z") -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": f"Body of {number}",
        "state": state,
        "labels": [],
        "assignees": [],
        "milestone": None,
        "user": {"login": "someone"},
        "created_at": "2026-07-20T09:00:00Z",
        "updated_at": updated,
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
    }


class Host:
    """A stand-in issues API that records what it was asked, by whom, and when.

    ``concurrent``/``peak`` are what the stampede test reads: the handler is async so
    two overlapping reads would both be inside it at once, and a sequential poll can
    never make ``peak`` exceed one.
    """

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.issues: dict[str, list[dict]] = {}
        self.requests: list[httpx.Request] = []
        self.concurrent = 0
        self.peak = 0
        self.fail_projects: set[str] = set()

    def for_project(self, project: str) -> list[dict]:
        return self.issues.setdefault(project, [])

    def _project_of(self, request: httpx.Request) -> str:
        # "/repos/{owner}/{repo}/issues" -> "{owner}/{repo}"
        parts = request.url.path.strip("/").split("/")
        return "/".join(parts[1:3]) if len(parts) >= _PROJECT_PATH_PARTS else ""

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            # A real, blocking pause — each provider read happens on its own thread
            # (``run_in_threadpool`` around a sync import), so an overlap is only
            # observable if the handler is still inside when the next one arrives.
            # 5ms is invisible sequentially and unmissable concurrently.
            time.sleep(0.005)
            self.requests.append(request)
            project = self._project_of(request)
            if self.status_code != 200 or project in self.fail_projects:
                code = self.status_code if self.status_code != 200 else _HTTP_SERVER_ERROR
                return httpx.Response(code, json={"message": "no"})
            if int(request.url.params.get("page", "1")) > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=self.for_project(project))
        finally:
            self.concurrent -= 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @property
    def projects_read(self) -> list[str]:
        return [self._project_of(r) for r in self.requests if r.url.path.endswith("issues")]

    @property
    def tokens_seen(self) -> set[str]:
        return {
            value.removeprefix("Bearer ").strip()
            for request in self.requests
            for name, value in request.headers.items()
            if name.lower() == "authorization"
        }


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_HMAC)


@pytest.fixture
def settings():
    """A deployment that polls, with the environment naming one project.

    ``import_poll_gap_seconds`` is left at the production default so the pacing
    under test is the shipped one, not a test-only value; the tests that would wait
    on it patch ``asyncio.sleep`` and assert what it was asked for.
    """
    return Settings(
        github_token=_A_TOKEN,
        github_repo=_A_PROJECT,
        github_api_url="https://gh.test",
        import_poll=True,
        import_poll_seconds=300.0,
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
def sleeps(monkeypatch):
    """Every ``asyncio.sleep`` the poll performs, recorded and not waited on.

    The pacing is a real two-second gap in production; a suite that actually slept
    it would take minutes, and a suite that set the gap to zero would stop testing
    the thing. So the delays are captured and asserted instead.
    """
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *args, **kwargs):
        # ``issue_import.asyncio`` IS the asyncio module, so this patch is global —
        # only the real waits are of interest, not every ``sleep(0)`` yield.
        if delay:
            recorded.append(delay)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


def _repositories(store: CardStore, projects: list[str], *, token: str) -> list[int]:
    """One connection with `projects` on it, credentialled with `token`."""
    connection = store.create_connection(GitConnectionCreate(provider="github"))
    store.set_connection_credential(connection.id, token)
    return [
        store.create_repository(connection.id, GitRepositoryCreate(project=p)).id for p in projects
    ]


def _poll(
    store: CardStore,
    settings: Settings,
    host_: Host,
    backoff: PollBackoff | None = None,
    *,
    lease_seconds: float = 0.0,
):
    """One poll cycle.

    ``lease_seconds=0`` by default so consecutive calls read as consecutive CYCLES —
    which is what a test means by calling this twice — rather than as a second
    replica inside one cycle. The replica tests pass a real window on purpose.
    """
    return asyncio.run(
        poll_once(
            store,
            settings,
            backoff=backoff if backoff is not None else PollBackoff(),
            lease_seconds=lease_seconds,
            transport=host_.transport(),
        )
    )


# ── the acceptance criterion: nobody calls an endpoint ───────────────────────


def test_an_issue_filed_after_the_last_pass_appears_on_the_next_poll(cards, host, settings):
    """#374's acceptance: an issue filed in a connected repository lands on the
    board without anyone calling an endpoint."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).extend([_issue(1), _issue(2)])

    _poll(cards, settings, host)
    assert sorted(c.issue_ref for c in cards.list()) == [f"{_A_PROJECT}#1", f"{_A_PROJECT}#2"]

    # Somebody files #3 five minutes later. Nobody clicks anything.
    host.for_project(_A_PROJECT).append(_issue(3, updated="2026-07-20T11:00:00Z"))
    _poll(cards, settings, host)

    refs = sorted(c.issue_ref for c in cards.list())
    assert refs == [f"{_A_PROJECT}#1", f"{_A_PROJECT}#2", f"{_A_PROJECT}#3"]
    # And still one card per issue: the poll is an upsert, not an insert.
    assert len(refs) == len(set(refs))


def test_an_issue_closed_upstream_moves_its_card_to_done_on_the_next_poll(cards, host, settings):
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    _poll(cards, settings, host)
    assert cards.list()[0].status == "backlog"

    host.issues[_A_PROJECT] = [_issue(1, state="closed", updated="2026-07-21T10:00:00Z")]
    _poll(cards, settings, host)

    card = cards.list()[0]
    assert (card.status, card.issue_state) == ("done", "closed")


@pytest.mark.usefixtures("sleeps")
def test_every_repository_of_every_connection_is_polled(cards, host, settings):
    """Per repository, not per tenant (#373 interaction): a tenant with repos on two
    connections reconciles both in one cycle."""
    _repositories(cards, [_A_PROJECT, "acme/gadgets"], token=_A_TOKEN)
    enterprise = cards.create_connection(
        GitConnectionCreate(provider="github", base_url="https://ghe.test", label="Enterprise")
    )
    cards.set_connection_credential(enterprise.id, _B_TOKEN)
    cards.create_repository(enterprise.id, GitRepositoryCreate(project="other/service"))

    results = _poll(cards, settings, host)

    assert [r["project"] for r in results] == [_A_PROJECT, "acme/gadgets", "other/service"]
    # Each repository was read on ITS OWN connection's host and credential.
    # A stored connection resolves its own host: the provider default for the first,
    # the enterprise base_url for the second.
    assert {r.url.host for r in host.requests} == {"api.github.com", "ghe.test"}
    assert host.tokens_seen == {_A_TOKEN, _B_TOKEN}


@pytest.mark.usefixtures("sleeps")
def test_a_repository_added_mid_run_is_picked_up_next_cycle(cards, host, settings):
    """The target list is re-read every cycle, not captured at startup — otherwise
    connecting a repository would need a restart to take effect."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    backoff = PollBackoff()

    first = _poll(cards, settings, host, backoff)
    assert [r["project"] for r in first] == [_A_PROJECT]

    # A second repository is connected while the poller is running.
    connection = cards.connections()[0]
    cards.create_repository(connection.id, GitRepositoryCreate(project="acme/gadgets"))
    host.for_project("acme/gadgets").append(_issue(9))

    second = _poll(cards, settings, host, backoff)

    assert [r["project"] for r in second] == [_A_PROJECT, "acme/gadgets"]
    assert "acme/gadgets#9" in {c.issue_ref for c in cards.list()}


# ── guard (a): tenant scoping ───────────────────────────────────────────────


@pytest.mark.usefixtures("sleeps")
def test_the_poll_never_reads_one_tenants_repository_with_anothers_credential(
    tmp_path, host, settings
):
    """MUTATION CHECK (a). Drop the tenant scope from the poll — enumerate every
    repository, or resolve one outside the caller's scope — and tenant A's
    credential is sent to tenant B's project. That is a cross-tenant credential
    leak, and it is what this test exists to make impossible to ship.
    """
    shared = CardStore(f"sqlite:///{tmp_path / 'shared.db'}")
    a = shared.scoped("tenant-a")
    b = shared.scoped("tenant-b")
    _repositories(a, [_A_PROJECT], token=_A_TOKEN)
    _repositories(b, [_B_PROJECT], token=_B_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    host.for_project(_B_PROJECT).append(_issue(2))

    results = _poll(a, settings, host)

    # Tenant A's poll read tenant A's project, with tenant A's credential, full stop.
    assert [r["project"] for r in results] == [_A_PROJECT]
    assert host.projects_read == [_A_PROJECT]
    assert host.tokens_seen == {_A_TOKEN}
    assert _B_TOKEN not in host.tokens_seen
    # And nothing of B's landed on A's board.
    assert [c.issue_ref for c in a.list()] == [f"{_A_PROJECT}#1"]
    assert b.list() == []


def test_polling_a_repository_outside_the_scope_resolves_nothing(tmp_path, host, settings):
    """The same guard one layer down: the id itself is not a capability."""
    shared = CardStore(f"sqlite:///{tmp_path / 'shared.db'}")
    a = shared.scoped("tenant-a")
    b = shared.scoped("tenant-b")
    _repositories(a, [_A_PROJECT], token=_A_TOKEN)
    (b_repo,) = _repositories(b, [_B_PROJECT], token=_B_TOKEN)

    with pytest.raises(Exception, match="no git repository"):
        import_issues(a, settings=settings, repository_id=b_repo, transport=host.transport())

    assert host.requests == []


# ── guard (b): the stampede bound ───────────────────────────────────────────


def test_a_tenant_with_many_repositories_does_not_stampede_the_provider(
    cards, host, settings, sleeps
):
    """MUTATION CHECK (b). Two properties, and losing either one is a stampede:

    * **concurrency** — the host never sees two reads at once. Swap the sequential
      loop for ``asyncio.gather`` and ``host.peak`` becomes 8 instead of 1.
    * **pacing** — a gap is awaited between repositories. Delete the
      ``asyncio.sleep(gap)`` and the recorded delays become empty, so eight reads
      leave in one tick.
    """
    projects = [f"acme/repo-{n}" for n in range(8)]
    _repositories(cards, projects, token=_A_TOKEN)
    for project in projects:
        host.for_project(project).append(_issue(1))

    results = _poll(cards, settings, host)

    assert len(results) == len(projects)
    assert host.peak == 1, "two repositories were read at the same time"
    # One gap per repository after the first, at the configured pace.
    assert sleeps == [settings.import_poll_gap_seconds] * (len(projects) - 1)
    assert sum(sleeps) >= settings.import_poll_gap_seconds * (len(projects) - 1)


@pytest.mark.usefixtures("sleeps")
def test_the_incremental_pass_stays_one_call_per_repository(cards, host, settings):
    """The poll must not get expensive: after the backfill, one page per repository
    per cycle, asking only for what changed."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    backoff = PollBackoff()
    _poll(cards, settings, host, backoff)
    host.requests.clear()

    _poll(cards, settings, host, backoff)

    assert len(host.requests) == 1
    params = host.requests[0].url.params
    assert params.get("since")  # the watermark, so the host returns only changes
    assert params.get("state") == "all"


# ── backoff, outages, and the board still serving reads ─────────────────────


@pytest.mark.usefixtures("sleeps")
def test_a_failing_repository_is_not_polled_every_cycle(cards, host, settings):
    """MUTATION CHECK (b), second half. Remove the backoff and a provider that is
    down is asked again at full cadence forever."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.fail_projects.add(_A_PROJECT)
    backoff = PollBackoff()

    first = _poll(cards, settings, host, backoff)
    assert first and first[0]["ok"] is False

    # The next cycle is skipped entirely — no request at all.
    host.requests.clear()
    assert _poll(cards, settings, host, backoff) == []
    assert host.requests == []

    # It comes back on its own once the provider does, with no operator action.
    host.fail_projects.clear()
    host.for_project(_A_PROJECT).append(_issue(1))
    recovered = _poll(cards, settings, host, backoff)
    assert recovered and recovered[0]["ok"] is True
    assert [c.issue_ref for c in cards.list()] == [f"{_A_PROJECT}#1"]


@pytest.mark.usefixtures("sleeps")
def test_a_rate_limited_host_backs_off_harder_than_a_broken_one(cards, settings):
    """429 means "fewer requests", so it enters the ladder above the first rung."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    limited = Host(status_code=_HTTP_TOO_MANY)
    backoff = PollBackoff()

    _poll(cards, settings, limited, backoff)

    # A plain failure sits out one cycle; a rate-limit refusal sits out several.
    skipped = 0
    while _poll(cards, settings, limited, backoff) == []:
        skipped += 1
        if skipped > _BACKOFF_CYCLE_GUARD:
            break
    assert skipped > 1, "a 429 backed off no harder than a generic failure"


def test_a_provider_outage_leaves_the_board_serving_reads(
    cards, audit, host, settings, monkeypatch
):
    """A provider outage degrades the board to stale. It never breaks it."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    _poll(cards, settings, host)

    down = Host(status_code=_HTTP_SERVER_ERROR)
    results = _poll(cards, settings, down)

    assert results and results[0]["ok"] is False
    assert results[0]["reason"]  # the reason is reported, not swallowed
    # The card imported before the outage is still readable, over HTTP, at 200.
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    auth.set_keys({_WRITER: {"read", "write"}})
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = down.transport
    try:
        client = TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
        listed = client.get("/api/cards")
        state = client.get("/api/cards/sync-state")
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert state.status_code == 200
        # A manual sync during the outage is a 200 with ok=false, not a 500.
        manual = client.post("/api/cards/import")
        assert manual.status_code == 200
        assert manual.json()["ok"] is False
    finally:
        auth.set_keys({})


def test_the_poll_loop_survives_a_cycle_that_raises(cards, settings, monkeypatch):
    """A background task that dies takes the board's reconciliation with it."""
    cycles = {"n": 0}

    async def exploding_poll(*_args, **_kwargs):
        cycles["n"] += 1
        if cycles["n"] == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    # Attached to the module logger rather than read off caplog: another suite in the
    # same session may have reconfigured propagation, and what is under test here is
    # that the failure is REPORTED, not which handler it reached.
    logged: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: logged.append(record.getMessage())  # type: ignore[method-assign]
    issue_import.logger.addHandler(handler)
    # Some other suite in this session leaves the module loggers disabled (a
    # dictConfig with disable_existing_loggers). Re-enable ours for the duration, or
    # this assertion silently depends on test ordering.
    was_disabled = issue_import.logger.disabled
    issue_import.logger.disabled = False

    real_sleep = asyncio.sleep
    monkeypatch.setattr(issue_import, "poll_once", exploding_poll)
    monkeypatch.setattr(asyncio, "sleep", lambda _d: real_sleep(0))

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(issue_import.poll_forever(cards, settings))
    finally:
        issue_import.logger.removeHandler(handler)
        issue_import.logger.disabled = was_disabled

    assert cycles["n"] == 2, "the loop stopped after the failing cycle"
    assert any("issue import poll failed" in message for message in logged)


# ── guard: one poller per cycle across replicas ─────────────────────────────


@pytest.mark.usefixtures("sleeps")
def test_a_second_replica_skips_the_cycle_the_first_one_claimed(cards, host, settings):
    """MUTATION CHECK: remove the lease claim and both replicas read every
    repository every cycle — twice the provider calls for the same board."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))

    window = settings.import_poll_seconds / 2
    first = _poll(cards, settings, host, lease_seconds=window)  # replica one claims
    host.requests.clear()
    # Replica two: same store, same moment, its own PollBackoff — a second process.
    second = _poll(cards, settings, host, lease_seconds=window)

    assert [r["project"] for r in first] == [_A_PROJECT]
    assert second == []
    assert host.requests == [], "the second poller read the host anyway"


def test_an_expired_lease_is_taken_by_the_next_poller(cards):
    """A replica killed mid-cycle costs that project one cycle, not an outage."""
    # A poller claims a window that is already behind it, then dies.
    assert cards.claim_import_lease(_A_PROJECT, -1.0) is True

    # The next poller finds the lease aged out and takes it...
    assert cards.claim_import_lease(_A_PROJECT, 300.0) is True
    # ...and while it holds it, nobody else does.
    assert cards.claim_import_lease(_A_PROJECT, 300.0) is False


def test_a_lease_is_per_project_not_per_board(cards):
    """Otherwise one slow repository would block every other repository's cycle."""
    assert cards.claim_import_lease(_A_PROJECT, 300.0) is True
    assert cards.claim_import_lease("acme/gadgets", 300.0) is True


def test_a_manual_sync_is_never_blocked_by_a_poll_lease(cards, host, settings):
    """The lease bounds the BACKGROUND poll. A human pressing Sync now is asking for
    a read here and now, and must never be told to wait for a lease they cannot see.
    """
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    assert cards.claim_import_lease(_A_PROJECT, 300.0) is True

    result = import_issues(cards, settings=settings, transport=host.transport())

    assert result["ok"] is True
    assert result["imported"] == 1


# ── the staleness read: last_synced_at surfaced ─────────────────────────────


@pytest.mark.usefixtures("sleeps")
def test_last_polled_at_advances_on_every_successful_pass(cards, host, settings):
    """Including a pass that found nothing new — which is most of them.

    The watermark cannot answer this: it is the newest issue ``updated_at`` seen, so
    on a repository nobody has touched it does not move, and a cockpit reading it
    would call every quiet board stale.
    """
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    backoff = PollBackoff()

    _poll(cards, settings, host, backoff)
    first = cards.import_states()[_A_PROJECT]
    assert first.last_polled_at is not None
    assert first.watermark_at is not None

    # A second pass with NOTHING new: same issue, same updated_at.
    _poll(cards, settings, host, backoff)
    second = cards.import_states()[_A_PROJECT]

    assert second.last_polled_at > first.last_polled_at
    assert second.watermark_at == first.watermark_at, "the cursor moved on an unchanged repo"


@pytest.mark.usefixtures("sleeps")
def test_a_failed_pass_does_not_pretend_the_board_is_current(cards, settings):
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)

    _poll(cards, settings, Host(status_code=_HTTP_SERVER_ERROR))

    assert cards.import_states().get(_A_PROJECT, None) is None or (
        cards.import_states()[_A_PROJECT].last_polled_at is None
    )
    state = card_ops.sync_state(cards, settings)
    assert state["repositories"][0]["stale"] is True


@pytest.mark.usefixtures("sleeps")
def test_sync_state_shows_every_repository_including_the_never_synced(cards, host, settings):
    ids = _repositories(cards, [_A_PROJECT, "acme/gadgets"], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    host.fail_projects.add("acme/gadgets")
    _poll(cards, settings, host)

    state = card_ops.sync_state(cards, settings)

    assert state["poll"] == {"enabled": True, "interval_seconds": 300.0, "live": False}
    by_id = {entry["repository_id"]: entry for entry in state["repositories"]}
    assert set(by_id) == set(ids)
    synced = by_id[ids[0]]
    assert synced["stale"] is False
    assert synced["last_polled_at"] is not None
    assert synced["is_default"] is True
    never = by_id[ids[1]]
    assert (never["last_polled_at"], never["stale"]) == (None, True)


def test_a_board_that_has_not_synced_for_two_cadences_reads_as_stale(cards, settings):
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    interval = settings.import_poll_seconds

    cards.mark_polled(_A_PROJECT, datetime.now(UTC) - timedelta(seconds=interval))
    assert card_ops.sync_state(cards, settings)["repositories"][0]["stale"] is False

    stale_by = interval * (_STALE_CYCLES + 1)
    cards.mark_polled(_A_PROJECT, datetime.now(UTC) - timedelta(seconds=stale_by))
    assert card_ops.sync_state(cards, settings)["repositories"][0]["stale"] is True


def test_sync_state_needs_no_repositories_to_answer(cards, settings):
    """A deployment on the environment-variable bridge still has a poll target, and
    a cockpit still has to render something for it."""
    state = card_ops.sync_state(cards, settings)

    assert [entry["project"] for entry in state["repositories"]] == [_A_PROJECT]
    assert state["repositories"][0]["repository_id"] is None
    assert state["repositories"][0]["stale"] is True


def test_sync_state_carries_no_credential(cards, settings):
    """The staleness read is a READ scope: it must not become a credential oracle."""
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    cards.set_git_config(GitConfigUpdate(project=_A_PROJECT))

    rendered = str(card_ops.sync_state(cards, settings))

    assert _A_TOKEN not in rendered
    assert "token" not in rendered.lower()


# ── the direction of truth, made deliberate ────────────────────────────────


@pytest.mark.usefixtures("sleeps")
def test_the_host_wins_on_mirrored_fields_and_the_board_keeps_its_own(cards, host, settings):
    """RFC-0003's direction of truth, pinned (#374).

    The repository is the record of truth, so an edit made on the BOARD to a mirrored
    field is overwritten by the next poll — deliberately, because two writers with no
    merge rule means one has to lose, and a board that won would silently contradict
    the issue. Everything the board owns survives. Change either half and this test
    says so, which is the point: the direction of truth is not something to adjust by
    accident.
    """
    _repositories(cards, [_A_PROJECT], token=_A_TOKEN)
    host.for_project(_A_PROJECT).append(_issue(1))
    backoff = PollBackoff()
    _poll(cards, settings, host, backoff)
    card = cards.list()[0]

    cards.update(
        card.card_key,
        {
            "title": "my local title",  # mirrored — will be lost
            "priority": 3,  # the board's own — will survive
            "acceptance_criteria": ["mine"],  # the board's own — will survive
        },
    )
    host.issues[_A_PROJECT] = [_issue(1, updated="2026-07-22T10:00:00Z")]
    _poll(cards, settings, host, backoff)

    after = cards.list()[0]
    assert after.title == "Issue 1", "the host did not win a mirrored field"
    assert after.priority == 3
    assert after.acceptance_criteria == ["mine"]
    # And the mirrored set is exactly what the module declares it to be, so the
    # documented direction of truth cannot drift from the code that enforces it.
    assert set(issue_import._mapping(_issue_data())) == issue_import.MIRRORED_FIELDS

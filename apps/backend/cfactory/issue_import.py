"""Import a repository's EXISTING issues into the board (RFC-0020 §3.6).

RFC-0019 Phase 6 sync is card-first: a card can open or adopt an issue, but
connecting a repo brings nothing in. Point the board at a repo with 200 open
issues and the board stays empty, which makes the feature useless to anyone who
already has a backlog. This module is the other direction — the repo's issues
become cards.

Everything here goes through the fleet's ``GitProvider`` protocol
(``fetch_issues(IssueFilters)``), so import works on GitLab and Azure DevOps and
not only on GitHub. Nothing below knows what a host's issue JSON looks like.

**Import is NOT live, but it does run on its own** (#374). There is still no
webhook receiver (RFC-0019 Phase 6 deferred inbound webhooks and RFC-0020 does not
undefer them), so an issue is on the board somewhere between zero and one poll
cadence after it is filed rather than the moment it is filed. What changed is that
nobody has to ask: :func:`poll_forever` runs by default, per repository, and
``last_synced_at`` is surfaced through ``GET /api/cards/sync-state`` so a stale
board says so instead of looking current.

The direction of truth is deliberate and one-way: see :data:`MIRRORED_FIELDS`.

Three properties are load-bearing, and each has a test named after it:

**An imported card is never ``ready``.** An open issue imports as ``backlog``, a
closed one as ``done``. ``ready`` + a tier is the dispatch trigger, and real
repositories are full of issues already labelled ``factory:low`` — importing them
as ``ready`` would fire a hundred builds from one "import" click. This is a
safety property, not a default: the status is computed by :func:`_status_for`,
which cannot return ``ready``.

**Import never duplicates.** The guard is the UNIQUE ``(tenant_id, issue_ref)``
index, not the lookup below it: an application-level "does this card exist?"
check loses a race between two concurrent polls, and a unique constraint does
not. The lookup is the fast path; the constraint is the truth, and a
:class:`~cfactory.cards.DuplicateIssueRefError` is handled by switching to an
update.

**A pull request never becomes a card.** ``include_prs`` is pinned ``False`` and
is not configurable, and the result is filtered again here — the protocol lets a
provider be sloppy about it, and this is cheap.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from runners.github.providers.protocol import IssueData, IssueFilters
from starlette.concurrency import run_in_threadpool

from .cards import Card, CardComment, CardCreate, CardStatus, CardStore, DuplicateIssueRefError
from .config import Settings, get_settings
from .error_ref import error_reference
from .git_config import PROJECT_RE
from .git_providers import IssueProvider, build_provider, run_sync
from .github_sync import IssueRef, sync_enabled

logger = logging.getLogger(__name__)

# RFC-0011's difficulty-tier label convention, reused verbatim: `factory:hard`,
# NOT `factory:high` — the tier vocabulary is low/medium/hard everywhere.
_TIER_PREFIX = "factory:"
_TIERS = frozenset({"low", "medium", "hard"})

# Priority an imported card lands at. Planning-only — the host has no opinion —
# so it is set once on import and never mirrored again: reprioritising an
# imported card must survive the next poll. 100 rather than 0 because the
# backlog sorts ascending, and a repo's existing issues should not shove a
# human's deliberately-planned cards down the board.
IMPORT_PRIORITY = 100

# How far back the watermark is rewound each pass, to tolerate clock skew
# between us and the host (RFC-0020 §3.6). Re-reading a few issues is free
# precisely because import is an upsert.
_OVERLAP = timedelta(seconds=60)

# Keys a provider's raw payload carries when the "issue" is really a pull /
# merge request. The belt to the protocol's braces.
_PR_KEYS = ("pull_request", "merge_request", "merge_requests_count")

# THE DIRECTION OF TRUTH, named rather than implied (#374). RFC-0003 makes the
# repository the record of truth and RFC-0019 §3.5 made that one line of policy;
# this is the exact set of card fields it costs, so the consequence is a constant
# a reader can find and a test can pin rather than a property of whatever
# :func:`_mapping` happens to return.
#
# **An edit made on the board to one of these fields is overwritten by the next
# poll.** That is intended, not a bug: two writers and no merge rule means one of
# them has to lose, and the one that loses is the board — the repository is where
# the issue is discussed, closed and reopened, and a board that could win would
# silently contradict it. Edit the issue, not the card. Everything NOT in this set
# is the board's own (priority, acceptance criteria, correlation key, stage runs)
# and survives every pass; ``status`` is conditionally the board's — see
# :func:`_changes_for`, which never moves a card the factory has touched.
MIRRORED_FIELDS = frozenset(
    {
        "title",
        "description",
        "labels",
        "tier",
        "assignee",
        "milestone",
        "issue_state",
        "github_sync_error",
    }
)

# Poll backoff bounds (#374). Eight cycles is forty minutes at the default
# cadence: long enough that a down provider is left alone, short enough that a
# board recovers on its own once the provider does, with nobody paged.
_MAX_BACKOFF_CYCLES = 8
# A rate-limit refusal enters the ladder at the third rung (four cycles), not the
# first: the host has explicitly said "fewer requests", and one cycle later is not
# an answer to that.
_RATE_LIMIT_FAILURES = 3


def _is_pull_request(issue: IssueData) -> bool:
    return any(key in issue.raw_data for key in _PR_KEYS)


def _tier_of(labels: list[str]) -> str | None:
    """The RFC-0011 tier a `factory:<tier>` label names, or None.

    Unknown ``factory:*`` labels (``factory:blocked``, say) are not tiers and are
    ignored rather than guessed at — a wrong tier routes a card to the wrong
    factory.
    """
    for label in labels:
        if label.lower().startswith(_TIER_PREFIX):
            tier = label.lower().removeprefix(_TIER_PREFIX).strip()
            if tier in _TIERS:
                return tier
    return None


def _status_for(issue_state: str) -> CardStatus:
    """The board column an issue's state imports as. **Never ``ready``.**

    See the module docstring: ``ready`` + a tier is the dispatch trigger, so an
    importer able to produce ``ready`` is an importer able to launch a hundred
    builds from one click. There is no branch here that can return it.
    """
    return "done" if issue_state == "closed" else "backlog"


def _mapping(issue: IssueData) -> dict[str, Any]:
    """The card fields an issue owns — RFC-0020 §3.6's mapping table, in code.

    All of these are MIRRORED: the host wins, on every pass, so a local edit to
    one of them is overwritten. That is the same one-line rule RFC-0019 §3.5
    established, extended to the fields import brings in.

    What is deliberately absent:

    * ``acceptance_criteria`` — **not** derived from the body. It is the list of
      testable statements dispatch turns into the RFC-0002 task contract's
      criteria; parsing prose into it would FABRICATE the thing the factory
      verifies against. Import leaves it empty (a legal planning state) and the
      board warns that the card is not dispatch-ready.
    * ``priority`` — planning-only, set once at create time.
    * ``correlation_key`` and ``stage_runs`` — the board's record of what the
      FACTORY was asked to do (RFC-0020 §3.7). An imported card has neither
      until somebody dispatches it, which is the point: importing a repository
      records issues, it does not claim work was started. The stage preconditions
      then refuse sensibly on their own — ``test`` on a freshly imported card
      answers ``no_build_to_verify``, because there is no completed code stage.
    """
    labels = [name for name in issue.labels if isinstance(name, str)]
    return {
        "title": issue.title or "(untitled issue)",
        "description": issue.body or None,
        "labels": labels,
        "tier": _tier_of(labels),
        "assignee": issue.assignees[0] if issue.assignees else None,
        "milestone": issue.milestone,
        "issue_state": issue.state or None,
        "github_sync_error": None,
    }


def _entered_the_factory(card: Card) -> bool:
    """Has this card ever been dispatched? Two signals, and both are needed.

    ``correlation_key`` alone is not enough since RFC-0020 §3.7 redefined it: it
    now means "the key this card's work is threaded on — reuse it", written by
    the FIRST stage that lands. A dispatch that *failed* leaves a ``stage_runs``
    record and a ``blocked`` card with **no** key at all, and a poll that read
    only the key would cheerfully un-block it and claim the work was never
    started.

    Nor is ``stage_runs`` alone enough: a card adopted from a pre-Phase-7 board
    carries a key and no per-stage record. Either signal means hands off.
    """
    return card.correlation_key is not None or bool(card.stage_runs)


def _changes_for(card: Card, issue: IssueData) -> dict[str, Any]:
    """The update an already-imported card takes from a later pass.

    Same mapping, minus the no-ops, plus the one rule that makes a poll safe to
    run against a live board: **a card the factory has touched keeps its
    status.** A run in flight — or a stage that failed and left the card blocked
    — owns that column, and a poll that stomped it back to ``backlog`` because
    somebody reopened the issue would contradict what the pipeline is actually
    doing. The issue's state still mirrors onto ``issue_state``, so nothing is
    hidden; only ``status`` is left alone.
    """
    changes = _mapping(issue)
    if not _entered_the_factory(card):
        changes["status"] = _status_for(issue.state)
    return {field: value for field, value in changes.items() if getattr(card, field) != value}


def _filters(settings: Settings, since: datetime | None) -> IssueFilters:
    """What to ask the provider for.

    The backfill default is **all open issues**, not a label-filtered subset:
    "connect my repo" means "show me my backlog", and a filter that quietly hides
    most of it produces a failure with no error to diagnose. ``import_labels``
    and ``import_state=all`` are opt-in.

    The incremental pass overrides the state to ``all`` regardless: closures and
    reopenings are exactly the changes a board would otherwise miss.
    """
    return IssueFilters(
        state="all" if since is not None else settings.import_state,
        labels=[label for label in settings.import_labels.split(",") if label.strip()],
        since=since,
        limit=max(1, settings.import_max),
        include_prs=False,  # Pinned. A pull request is not a plan.
    )


def _next_watermark(issues: list[IssueData], previous: datetime | None) -> datetime | None:
    """The ``since`` for the next pass: newest ``updated_at`` seen, minus the overlap."""
    seen = [issue.updated_at for issue in issues if isinstance(issue.updated_at, datetime)]
    if not seen:
        return previous
    newest = max(stamp.astimezone(UTC) for stamp in seen)
    return newest - _OVERLAP


def _import_one(
    store: CardStore, ref: IssueRef, issue: IssueData, repository_id: int | None = None
) -> str:
    """Upsert one issue into a card. Returns what happened, for the tally.

    ``skipped`` covers the one case where an issue must NOT produce a card: the
    human soft-deleted it off the board. Resurrecting it on the next poll is the
    failure RFC-0019 already names as a risk, and it is why the delete is soft.
    """
    existing = store.get_by_issue_ref(str(ref))
    if existing is None:
        fields = _mapping(issue)
        try:
            card = store.create(
                CardCreate(
                    issue_ref=str(ref),
                    status=_status_for(issue.state),
                    priority=IMPORT_PRIORITY,
                    title=str(fields["title"]),
                    description=fields["description"],
                    tier=fields["tier"],
                    assignee=fields["assignee"],
                    milestone=fields["milestone"],
                    # The repository it came FROM (RFC-0020 §3.3 phase 8), so an
                    # imported card syncs, dispatches and builds against that
                    # repository rather than against whatever the tenant's default
                    # happens to be later.
                    repository_id=repository_id,
                )
            )
            # The two mirrored columns are absent from CardCreate on purpose — a
            # caller must not be able to assert something the host never said —
            # so a freshly created card picks them up here.
            store.update(
                card.card_key,
                {"labels": fields["labels"], "issue_state": fields["issue_state"]},
            )
            return "imported"
        except DuplicateIssueRefError:
            # A concurrent import won the race. The constraint did its job; this
            # pass switches to updating the row that landed.
            existing = store.get_by_issue_ref(str(ref))
            if existing is None:  # pragma: no cover — the row must exist by now
                return "skipped"

    if existing.deleted_at is not None:
        return "skipped"
    changes = _changes_for(existing, issue)
    if changes:
        store.update(existing.card_key, changes)
    return "updated"


# How far back the comment refresh's ``since`` is rewound, for the same clock-skew
# reason as the issue watermark's :data:`_OVERLAP`. Wider than that one because a
# missed comment is not re-offered by a later pass the way a missed issue is: the
# next pass asks from a NEWER cursor, so anything that fell through the skew gap is
# gone until that comment is edited. Re-reading a few minutes of thread is one page.
_COMMENT_OVERLAP = timedelta(minutes=5)


def _sync_comments(
    store: CardStore,
    provider: IssueProvider,
    project: str,
    settings: Settings,
) -> dict[str, Any]:
    """Refresh the stored comment threads for one project's cards (Factory#375).

    Wired into the import rather than given a mechanism of its own, so there is
    ONE sync path: whatever brings the issue in — a manual ``POST /api/cards/import``
    or the #374 background poll — brings its discussion with it, on the SAME
    provider, built from THAT repository's connection. A card on a GitLab
    repository is only ever visited by the GitLab pass, because the cards are
    selected by the project the pass is reading.

    Two groups, because they cost different things:

    * **warm** — cards whose thread has been read in full before. One
      ``fetch_comments_bulk`` with a ``since``, which on GitHub is ONE request for
      the entire board (the repository-wide comments endpoint) and on a provider
      with no bulk endpoint is the hub's bounded per-issue fan-out.
    * **cold** — cards never read. There is no window to narrow, so each costs a
      call; :attr:`Settings.import_comment_backfill_max` bounds how many one pass
      takes and the rest arrive on later passes. Their ``since`` is ``None``:
      asking for "everything since the poll watermark" on a thread we have never
      seen would store a fragment and mark it whole.

    Failure is per group and NEVER partial. If a read raises, that group's cards
    keep the copy and the marker they already had, and the reason is reported —
    an issue with no discussion and an issue whose discussion failed to download
    must stay distinguishable, and the only thing that distinguishes them is that
    the second one's ``comments_synced_at`` is still NULL.
    """
    cards = [card for card in store.cards_for_project(project) if card.issue_ref]
    if not cards:
        return {"ok": True, "cards": 0, "comments": 0, "backfilled": 0, "refreshed": 0}

    by_number: dict[int, Card] = {}
    for card in cards:
        ref = IssueRef.parse(card.issue_ref)
        if ref is not None:
            by_number[ref.number] = card

    warm = [n for n, card in by_number.items() if card.comments_synced_at is not None]
    cold = [n for n, card in by_number.items() if card.comments_synced_at is None]
    # Oldest cards first, so a backlog backfills in the order it was imported
    # rather than in whatever order the dict happens to hold.
    cold = sorted(cold)[: max(0, settings.import_comment_backfill_max)]

    since = None
    if warm:
        # The oldest complete copy in the group decides the window: a card synced
        # an hour ago and one synced a minute ago share one request, and asking
        # from the newer cursor would leave the older card with a hole.
        oldest = min(
            card.comments_synced_at
            for card in by_number.values()
            if card.comments_synced_at is not None
        )
        # SQLite hands back a naive datetime even for an aware one; the providers
        # compare this against aware provider timestamps.
        since = (oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)) - _COMMENT_OVERLAP

    stored = 0
    reasons: list[str] = []
    for numbers, window in ((warm, since), (cold, None)):
        if not numbers:
            continue
        try:
            threads = run_sync(provider.fetch_comments_bulk(sorted(numbers), window))
        except Exception as exc:  # noqa: BLE001 — one group's provider failure must
            # not abandon the other, and must not take the import down. Reported,
            # never swallowed: nothing is stored and no marker moves, so the cards
            # keep saying "unknown" rather than "no discussion".
            # The full failure to the log, a correlation id to the caller: this
            # reason reaches an API response, and provider/stdlib exception text
            # names internal hosts and paths (CWE-209, same class as the two
            # CodeQL flagged in routes_install / routes_cards).
            error_id = error_reference(logger, f"comment read failed for {project}", exc)
            reasons.append(f"the provider call failed (reference {error_id})")
            continue
        synced_at = datetime.now(UTC)
        for number, comments in threads.items():
            target = by_number.get(number)
            if target is None:  # pragma: no cover — we asked for these numbers
                continue
            try:
                stored += store.store_comments(
                    target.card_key,
                    [
                        CardComment(
                            comment_id=comment.id,
                            author=comment.author,
                            body=comment.body,
                            url=comment.url,
                            created_at=comment.created_at,
                            updated_at=comment.updated_at,
                        )
                        for comment in comments
                    ],
                    synced_at=synced_at,
                )
            except Exception as exc:  # noqa: BLE001 — one card's write must not
                # abandon the rest of the thread pass; the marker stays unset, so
                # the next pass retries this card.
                logger.warning("could not store comments for %s: %s", target.card_key, exc)
                reasons.append(f"{target.card_key}: {type(exc).__name__}")
    return {
        "ok": not reasons,
        "cards": len(warm) + len(cold),
        "comments": stored,
        "backfilled": len(cold),
        "refreshed": len(warm),
        # Every card whose thread is still unknown after this pass — the honest
        # count of what the board does NOT have, rather than silence.
        "pending": max(0, len(by_number) - len(warm) - len(cold)),
        **({"reason": "; ".join(reasons)[:512]} if reasons else {}),
    }


def import_issues(  # noqa: PLR0913 — every parameter after `store` is keyword-only
    # and independent (which repository, which project, which transport, full or
    # incremental). Collapsing them into an options object would hide the call
    # sites rather than simplify them, the same reasoning routes_cards.import_cards
    # records for its own signature.
    store: CardStore,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    project: str | None = None,
    repository_id: int | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Import ONE REPOSITORY's issues as cards. Never raises.

    Which repository (RFC-0020 §3.3 phase 8, and §3.6's import made per-repository
    rather than per-tenant): ``repository_id`` if given, else the repository whose
    project path matches ``project``, else the tenant's default. The provider, the
    host and the credential come from THAT repository's connection, which is what
    lets one tenant import from four repositories across two providers by calling
    this four times.

    Incremental by default: after the first pass a ``last_synced_at`` watermark
    exists and only issues updated since (minus a 60s clock-skew overlap) are
    asked for. ``full=True`` ignores the watermark and re-reads everything, which
    is safe at any time because the import is an upsert. The watermark is per
    (tenant, project) already, so two repositories never share one.

    Same fail-safe contract as ``github_sync.sync_card``: a provider outage
    returns ``ok=False`` with the reason, rather than 500ing the board.
    """
    settings = settings or get_settings()
    git = store.git_target_for(settings, repository_id=repository_id, project=project)
    if not sync_enabled(git):
        return _result(project or "", ok=True, reason="git provider sync not configured")

    # The repository's intake project is where a backfill READS from, falling back
    # to its own path (RFC-0020 §3.3). An explicit argument still wins — the import
    # endpoint takes a project so a one-off pull from a repo the tenant has not
    # configured at all is possible, on the resolved connection.
    target = (project or git.import_project or "").strip()
    if PROJECT_RE.match(target) is None:
        return _result(
            target,
            ok=False,
            reason=(
                "cannot import: no project is configured for this tenant — add a repository "
                "in Settings > Git integration, or pass one explicitly"
            ),
        )

    since = None if full else store.get_watermark(target)
    filters = _filters(settings, since)
    try:
        # Inside the try since RFC-0020 phase 4: building a provider RESOLVES the
        # credential, and on an installed connection that mints a short-lived
        # token — which can fail (a refresh the provider refuses). That failure is
        # loud by design and must land in this function's documented
        # ``ok=False, reason`` result rather than escaping a never-raises contract
        # the background poll depends on.
        provider = build_provider(git, target, transport=transport)
        issues = run_sync(provider.fetch_issues(filters))
    except Exception as exc:  # noqa: BLE001 — the never-raises contract, same as
        # github_sync.sync_card: behind the protocol sits third-party provider
        # code we do not control, and an unlisted exception type must not take
        # the board down. Nothing is swallowed — the FULL failure is logged, and
        # a correlation id is returned and audited by the caller. The exception's
        # own text is not returned: it reaches an API response and names internal
        # hosts and paths (CWE-209).
        error_id = error_reference(logger, f"issue import failed for {target}", exc)
        return _result(
            target,
            ok=False,
            reason=f"the provider call failed (reference {error_id})",
            rate_limited=_looks_rate_limited(exc),
        )

    # The provider was asked for issues only; asserting it again is one line and
    # covers a host that answers with merge requests anyway.
    issues = [issue for issue in issues if not _is_pull_request(issue)]

    tally = {"imported": 0, "updated": 0, "skipped": 0}
    for issue in issues:
        ref = IssueRef(target, issue.number)
        try:
            tally[_import_one(store, ref, issue, git.repository_id)] += 1
        except Exception as exc:  # noqa: BLE001 — one malformed issue must not
            # abandon the other 199. Counted as skipped and logged.
            logger.warning("could not import %s: %s", ref, exc)
            tally["skipped"] += 1

    # After the cards exist, so an issue imported a moment ago gets its thread on
    # the same pass rather than on the next one. Never raises: its own failures are
    # reported inside the block it returns, and a comment outage must not make an
    # otherwise-good issue import look failed.
    comments = (
        _sync_comments(store, provider, target, settings)
        if settings.import_comments
        else {"ok": True, "enabled": False}
    )

    watermark = _next_watermark(issues, since)
    if watermark is not None:
        store.set_watermark(target, watermark)
    # Recorded on every SUCCESSFUL pass, including one that found nothing: a pass
    # with no changes still proves the board is current, and the watermark above
    # cannot say so — it does not move on a repository nobody has touched (#374).
    polled_at = datetime.now(UTC)
    store.mark_polled(target, polled_at)

    # A truncated import is REPORTED, never silent: a board that quietly holds
    # the first 1000 of 3000 issues looks complete and is not.
    truncated = len(issues) >= filters.limit
    return _result(
        target,
        ok=True,
        incremental=since is not None,
        seen=len(issues),
        truncated=truncated,
        import_max=filters.limit,
        # The incremental cursor, kept under its established name. What a human
        # means by "last synced" is `polled_at` beside it.
        last_synced_at=watermark.isoformat() if watermark else None,
        polled_at=polled_at.isoformat(),
        # Reported as its own block rather than folded into the tally: comments can
        # fail while the issue import succeeded, and flattening the two would hide
        # exactly the case that matters (Factory#375).
        comments=comments,
        **tally,
    )


class PollBackoff:
    """Per-project failure memory, counted in CYCLES rather than in seconds (#374).

    A provider that is down, rate-limited or misconfigured must not be asked again
    at the same cadence forever — that is how a poll turns one outage into a
    sustained hammering. Backoff is counted in whole poll cycles because the loop
    already has a clock (the cadence itself): a project that failed once sits out
    one cycle, then two, then four, capped at :data:`_MAX_BACKOFF_CYCLES`, and a
    single success clears the memory.

    A rate-limit refusal is not treated as a first failure but as the third, so the
    board waits four cycles rather than one before spending another request on a
    host that has just said no. The provider's own ``Retry-After`` is deliberately
    NOT parsed: ``import_issues`` flattens every failure to a reason string (its
    never-raises contract), and inventing a parser for a header this layer cannot
    see would be guessing dressed up as precision. The cap is the honest bound.

    ponytail: cycles, not wall clock. A monotonic deadline per project would be
    finer-grained and would need a clock injected into every test for no behavioural
    gain — the cadence IS the resolution anyone can observe.
    """

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}
        self._skip: dict[str, int] = {}

    def due(self, key: str) -> bool:
        """Is this project due a poll this cycle? Consumes one cycle of backoff."""
        remaining = self._skip.get(key, 0)
        if remaining > 0:
            self._skip[key] = remaining - 1
            return False
        return True

    def record(self, key: str, result: dict[str, Any]) -> None:
        """Fold one import result in: success forgets, failure backs off."""
        if result.get("ok"):
            self._failures.pop(key, None)
            self._skip.pop(key, None)
            return
        failures = self._failures.get(key, 0) + 1
        if _is_rate_limited(result):
            failures = max(failures, _RATE_LIMIT_FAILURES)
        self._failures[key] = failures
        self._skip[key] = min(2 ** (failures - 1), _MAX_BACKOFF_CYCLES)


def _is_rate_limited(result: dict[str, Any]) -> bool:
    """Did the host ask us to slow down?

    Reads the flag :func:`import_issues` set from the EXCEPTION, not the
    ``reason`` string. It used to grep the reason for "429" - which coupled the
    backoff ladder to the wording of a human-facing message, and duly broke the
    moment that message stopped quoting the provider's error (CWE-209: it named
    internal hosts and paths). A machine decision reads a machine field.
    """
    return bool(result.get("rate_limited"))


def _looks_rate_limited(exc: BaseException) -> bool:
    """Does this exception carry the host's "too many requests" refusal?"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _lease_key(project: str | None) -> str:
    """The lease/backoff identity of one poll target: the project it reads from."""
    return (project or "").strip()


async def poll_once(
    store: CardStore,
    settings: Settings,
    *,
    backoff: PollBackoff,
    lease_seconds: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """One reconciliation pass over every repository this tenant has (#374).

    Three properties, and each has a test named after it:

    **One repository at a time.** The loop is sequential and paced by
    ``CFACTORY_IMPORT_POLL_GAP_SECONDS`` between repositories, so a tenant with
    forty repositories spreads forty cheap reads across the cycle instead of firing
    them at one host in one tick. That is the whole stampede guard: a concurrency
    bound of one, plus a gap. Replace this loop with an ``asyncio.gather`` and
    ``test_a_tenant_with_many_repositories_does_not_stampede_the_provider`` fails.

    **One poller per project per cycle.** Every target is claimed through
    :meth:`CardStore.claim_import_lease` first, so a second replica waking at the
    same moment skips what the first is already reading. Advisory, expiring, and not
    a correctness boundary — the unique ``(tenant_id, issue_ref)`` index is.

    **Tenant scoping is the store's, not this loop's.** The repositories come from
    ``store.repositories()`` (tenant-filtered) and each one's host and credential
    from ``store.git_target_for(repository_id=...)`` (tenant-filtered again), so
    there is no path here by which one tenant's repository is read with another
    tenant's credential. Poll an id the store did not hand out and the resolve
    raises rather than reaching for the default.

    ``lease_seconds`` is the width of THIS cycle's claim window; ``None`` derives it
    from the cadence (:func:`_lease_seconds`). Zero means "do not hold a lease",
    which is what a single-replica deployment could run with and what makes each
    call in a test a fresh cycle rather than a second poller inside one.

    Returns one result per target actually polled — skipped ones are absent — so a
    caller (and the test suite) can see what a cycle did.
    """
    lease = _lease_seconds(settings) if lease_seconds is None else lease_seconds
    repositories = await run_in_threadpool(store.repositories)
    # (repository_id, project) per target, and [(None, env project)] when the tenant
    # has no repositories: one pass that resolves against the deployment's
    # environment variables, exactly as before RFC-0020 phase 8.
    targets: list[tuple[int | None, str]] = [
        (repo.id, _lease_key(repo.intake_project or repo.project)) for repo in repositories
    ] or [(None, _lease_key(settings.github_repo))]

    results: list[dict[str, Any]] = []
    for index, (repository_id, key) in enumerate(targets):
        if not backoff.due(key):
            logger.debug("issue import poll: backing off %s", key or "(unconfigured)")
            continue
        if key and not await run_in_threadpool(store.claim_import_lease, key, lease):
            logger.debug("issue import poll: %s is another poller's this cycle", key)
            continue
        if index and settings.import_poll_gap_seconds > 0:
            # The pace. Before the call, not after, so the first repository of a
            # cycle is not delayed and the last one is not followed by dead time.
            await asyncio.sleep(settings.import_poll_gap_seconds)
        result = await run_in_threadpool(
            import_issues,
            store,
            settings=settings,
            repository_id=repository_id,
            transport=transport,
        )
        backoff.record(key, result)
        logger.info("issue import poll: %s", result)
        results.append(result)
    return results


def _lease_seconds(settings: Settings) -> float:
    """How long a claimed cycle is held: HALF a cadence.

    Not a whole one, and the difference matters. A lease as wide as the cadence
    expires at the exact moment the next cycle begins, so the smallest drift makes a
    replica skip its own next cycle — the poll would then run at half rate, or at
    nothing, for reasons no log would explain.

    Half a cadence is what the lease is actually for: two replicas that woke at
    roughly the same moment must not both read. Replicas offset by more than that are
    not a stampede — they are one poll per project per cadence, which is the design.
    """
    return max(1.0, settings.import_poll_seconds) / 2


async def poll_forever(
    store: CardStore, settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Re-import on a cadence, so issues filed after connect show up (#374).

    This is the closest thing to "live" that exists without a webhook receiver, and
    it is not live: an issue is on the board somewhere between zero and
    ``CFACTORY_IMPORT_POLL_SECONDS`` (default 300) after it is filed. The cockpit
    shows ``last_synced_at`` for exactly that reason — a board that might be five
    minutes stale is fine, a board that is silently five days stale is not.

    ON by default since #374 (``CFACTORY_IMPORT_POLL=false`` stops it), and inert
    without a configured repository: the pass resolves nothing and calls nobody.

    A pass never raises. ``import_issues`` already flattens a provider outage into
    ``ok=False`` plus a reason, and anything else this loop can hit is caught below
    — a background task that dies takes the board's reconciliation with it, and the
    board must degrade to "stale" rather than to "never syncs again".

    ponytail: this store's tenant only. Multi-tenant polling wants a tenant
    enumeration this store does not have; a hosted multi-tenant deploy would give
    each tenant its own loop, or this one a tenant list.
    """
    backoff = PollBackoff()
    while True:
        await asyncio.sleep(max(1.0, settings.import_poll_seconds))
        try:
            await poll_once(store, settings, backoff=backoff, transport=transport)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad cycle end the loop: without this the task dies with
            # it and the board silently stops reconciling; logged and retried.
            logger.exception("issue import poll failed")


def _result(project: str, *, ok: bool, **extra: Any) -> dict[str, Any]:
    """One shape for every return, so a caller never has to test for a key."""
    return {
        "ok": ok,
        "project": project,
        "imported": 0,
        "updated": 0,
        "skipped": 0,
        "seen": 0,
        "truncated": False,
        "incremental": False,
        "polled_at": None,  # Set only on a successful pass — see import_issues.
        # The comment pass (Factory#375). Present on every result so a caller never
        # has to test for the key; ``ok`` false with a reason when the thread read
        # failed, in which case nothing was stored and no card claims completeness.
        "comments": {"ok": True, "cards": 0, "comments": 0},
        "live": False,  # Poll-based, by design. See the module docstring.
        # Set from the EXCEPTION on a failed pass, so the poll's backoff ladder
        # never has to grep a human-facing reason string (see _is_rate_limited).
        "rate_limited": False,
        **extra,
    }

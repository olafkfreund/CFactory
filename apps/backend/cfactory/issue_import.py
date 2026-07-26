"""Import a repository's EXISTING issues into the board (RFC-0020 §3.6).

RFC-0019 Phase 6 sync is card-first: a card can open or adopt an issue, but
connecting a repo brings nothing in. Point the board at a repo with 200 open
issues and the board stays empty, which makes the feature useless to anyone who
already has a backlog. This module is the other direction — the repo's issues
become cards.

Everything here goes through the fleet's ``GitProvider`` protocol
(``fetch_issues(IssueFilters)``), so import works on GitLab and Azure DevOps and
not only on GitHub. Nothing below knows what a host's issue JSON looks like.

**Import is NOT live.** There is no webhook receiver (RFC-0019 Phase 6 deferred
inbound webhooks and RFC-0020 does not undefer them), so an issue filed a second
ago appears when someone — a human, the MCP tool, or the optional poller — *asks*
for an import. A board can be stale; ``last_synced_at`` is returned and stored so
the staleness is visible rather than assumed away.

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

from .cards import Card, CardCreate, CardStatus, CardStore, DuplicateIssueRefError
from .config import Settings, get_settings
from .git_config import PROJECT_RE
from .git_providers import build_provider, run_sync
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


def _import_one(store: CardStore, ref: IssueRef, issue: IssueData) -> str:
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


def import_issues(
    store: CardStore,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    project: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Import a project's issues as cards. Never raises.

    Incremental by default: after the first pass a ``last_synced_at`` watermark
    exists and only issues updated since (minus a 60s clock-skew overlap) are
    asked for. ``full=True`` ignores the watermark and re-reads everything, which
    is safe at any time because the import is an upsert.

    Same fail-safe contract as ``github_sync.sync_card``: a provider outage
    returns ``ok=False`` with the reason, rather than 500ing the board.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return _result(project or "", ok=True, reason="git provider sync not configured")

    git = store.git_target(settings)
    # The tenant's intake project is where a backfill READS from, falling back to
    # the sync project (RFC-0020 §3.3). An explicit argument still wins — the
    # import endpoint takes a project so a one-off pull from another repo does
    # not require editing the tenant's configuration first.
    target = (project or git.import_project or "").strip()
    if PROJECT_RE.match(target) is None:
        return _result(
            target,
            ok=False,
            reason=(
                "cannot import: no project is configured for this tenant — set one in "
                "Settings > Git integration, or pass one explicitly"
            ),
        )

    since = None if full else store.get_watermark(target)
    filters = _filters(settings, since)
    try:
        issues = run_sync(build_provider(git, target, transport=transport).fetch_issues(filters))
    except Exception as exc:  # noqa: BLE001 — the never-raises contract, same as
        # github_sync.sync_card: behind the protocol sits third-party provider
        # code we do not control, and an unlisted exception type must not take
        # the board down. Nothing is swallowed — the reason is returned, logged,
        # and audited by the caller.
        reason = f"{type(exc).__name__}: {exc}"[:512]
        logger.warning("issue import failed for %s: %s", target, reason)
        return _result(target, ok=False, reason=reason)

    # The provider was asked for issues only; asserting it again is one line and
    # covers a host that answers with merge requests anyway.
    issues = [issue for issue in issues if not _is_pull_request(issue)]

    tally = {"imported": 0, "updated": 0, "skipped": 0}
    for issue in issues:
        ref = IssueRef(target, issue.number)
        try:
            tally[_import_one(store, ref, issue)] += 1
        except Exception as exc:  # noqa: BLE001 — one malformed issue must not
            # abandon the other 199. Counted as skipped and logged.
            logger.warning("could not import %s: %s", ref, exc)
            tally["skipped"] += 1

    watermark = _next_watermark(issues, since)
    if watermark is not None:
        store.set_watermark(target, watermark)

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
        last_synced_at=watermark.isoformat() if watermark else None,
        **tally,
    )


async def poll_forever(store: CardStore, settings: Settings) -> None:
    """Re-import on a cadence, so issues filed after connect show up.

    This is the closest thing to "live" that exists without a webhook receiver,
    and it is not live: an issue is on the board somewhere between zero and
    ``CFACTORY_IMPORT_POLL_SECONDS`` (default 300) after it is filed. The board
    shows ``last_synced_at`` for exactly that reason.

    Off unless ``CFACTORY_IMPORT_POLL`` is set, like every other background loop
    here — a deploy that configured a token opted into syncing cards, not into a
    background job hitting someone's API every five minutes.

    ponytail: polls the default tenant's configured project only. A per-tenant
    git config (RFC-0020 §3.3) is the phase that owns "which repos does tenant X
    import from"; when it lands, loop over its rows here.
    """
    while True:
        await asyncio.sleep(max(1.0, settings.import_poll_seconds))
        try:
            result = await run_in_threadpool(import_issues, store, settings=settings)
            logger.info("issue import poll: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
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
        "live": False,  # Poll-based, by design. See the module docstring.
        **extra,
    }

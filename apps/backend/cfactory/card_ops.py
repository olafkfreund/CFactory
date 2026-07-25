"""Board operations — ONE implementation per card mutation (RFC-0019 §3.3).

The RFC's design law is "every board action a human can take has an identical
REST + MCP equivalent". Two call sites, two transports, but the *operation* must
be single-sourced: if the audit stamp or the not-found rule lived in
``routes_cards`` and again in ``mcp``, parity would be a coincidence rather than
a property.

So both transports call these functions. Each takes the tenant-scoped
:class:`~cfactory.cards.CardStore`, an :class:`AuditContext` saying who is
calling and over which surface, and raises transport-neutral errors that the
caller renders: ``routes_cards`` turns them into 404/409, ``mcp`` into a JSON
``{"error": ...}`` payload (matching how its read tools already report a missing
key).

That single-sourcing covers the RFC-0019 §3.2 **intake hook** too, not just the
CRUD: a write that leaves a card ``ready`` with a tier dispatches it into the
factory. Leaving the dispatch in the REST route would mean an agent moving a
card to ``ready`` over MCP silently never entered it into the factory — the
board would agree between the two surfaces while the *pipeline* did not, which
is precisely the failure §3.3 exists to prevent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import httpx

from .audit import AuditStore
from .card_intake import dispatch_stage, maybe_dispatch, parse_stage, start_sequence
from .cards import Card, CardCreate, CardStore, CardUpdate
from .github_sync import maybe_sync, sync_card
from .issue_import import import_issues

# Audit rows carry a ``target_service``; a card mutation never leaves CFactory,
# so it is attributed to CFactory itself rather than to an upstream factory.
_SELF = "cfactory"

# Audit ``endpoint`` prefix for a mutation that arrived over REST. The MCP
# transport passes its own so the trail says which surface it came in through.
REST_ENDPOINT = "/api/cards"

# Audit actor for a dispatch nobody typed: a sequenced card advancing to its next
# stage on the completion event that finished the previous one (RFC-0020 §3.7).
SEQUENCE_ACTOR = "cfactory-sequence"


class CardNotFoundError(Exception):
    """No card with that key exists *in the caller's tenant scope*."""


class StageRefusedError(Exception):
    """A stage action that must not be dispatched (RFC-0020 §3.7).

    Carries the machine-readable ``code`` and the human ``message``: the REST
    route renders it as a 409, MCP as an ``{"error": ..., "reason": ...}``
    payload. Transport-neutral like :class:`CardNotFoundError`, so the refusal
    rules live in one place and both surfaces are refused identically.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UnknownStageError(Exception):
    """The caller named something that is not a plan/code/test stage."""


@dataclass(frozen=True)
class AuditContext:
    """Provenance for a mutation: the chain to append to, who did it, from where.

    These three always travel together — an audit entry is meaningless without
    all of them — so they are passed as one value rather than threaded through
    every operation as three separate arguments.
    """

    audit: AuditStore
    actor: str
    endpoint: str = REST_ENDPOINT


def _record(ctx: AuditContext, *, kind: str, card_key: str, status_code: int) -> None:
    """Append a card mutation to the shared HITL audit chain.

    The chain's ``correlation_key`` column is the entity key of the thing that
    was mutated; for a card that is its ``card_key`` (which is *not* yet an
    RFC-0001 correlation key — a planned card has none until it enters the
    factory).
    """
    ctx.audit.record(
        actor=ctx.actor,
        kind=kind,
        correlation_key=card_key,
        target_service=_SELF,
        endpoint=f"{ctx.endpoint}/{card_key}",
        status_code=status_code,
        ok=True,
    )


def _intake(
    store: CardStore,
    ctx: AuditContext,
    card: Card,
    transport: httpx.BaseTransport | None,
) -> Card:
    """Run the RFC-0019 §3.2 intake hook and return the card as it now stands.

    A no-op unless this write left the card ``ready`` with a tier and not yet
    joined to a work item. The dispatch itself never raises (see
    ``card_intake.dispatch_card``), so a failing upstream turns into a blocked
    card plus an ``ok=False`` audit entry — surfaced, never swallowed, and never
    a 500 on the caller's PATCH (nor a JSON-RPC internal error on an MCP call).

    Writes the audit entry directly rather than through :func:`_record`: unlike
    a CRUD entry this one is attributed to the UPSTREAM factory and may carry
    ``ok=False``, which are the two things ``_record`` fixes.
    """
    result = maybe_dispatch(store, card, transport=transport)
    if result is None:
        return card
    record_dispatch(ctx, card.card_key, result)
    return store.get(card.card_key) or card


def record_dispatch(ctx: AuditContext, card_key: str, result: dict[str, Any]) -> None:
    """Audit one dispatch into the factory — implicit promotion, explicit stage
    action, or a sequence advancing itself on a completion event.

    Attributed to the UPSTREAM factory and may carry ``ok=False``, which are the
    two things :func:`_record` fixes, so it is a separate writer. One
    implementation for all three sources: an auto-advanced stage leaves exactly
    the entry a human's button press would.
    """
    ctx.audit.record(
        actor=ctx.actor,
        kind="dispatch_card",
        correlation_key=str(result.get("correlation_key") or card_key),
        target_service=str(result.get("target_service", _SELF)),
        endpoint=f"{ctx.endpoint}/{card_key}",
        status_code=int(result.get("status_code", 0)),
        ok=bool(result.get("ok", False)),
    )


def dispatch_recorder(ctx: AuditContext) -> Callable[[str, dict[str, Any]], None]:
    """The audit sink for a dispatch nobody typed: the event ingress hands this to
    ``card_intake.apply_status`` so a sequence's own advance is audit-chained too."""

    def sink(card_key: str, result: dict[str, Any]) -> None:
        record_dispatch(ctx, card_key, result)

    return sink


def _github(
    store: CardStore,
    ctx: AuditContext,
    card: Card,
    transport: httpx.BaseTransport | None,
) -> Card:
    """Run the RFC-0019 §3.5 GitHub hook and return the card as it now stands.

    A no-op unless sync is configured AND this write left the card ``ready`` (or
    already carrying an issue). Like the intake hook it never raises: a GitHub
    outage marks the card and writes an ``ok=False`` audit entry rather than
    500ing the board.

    Runs BEFORE the intake dispatch, so a card that enters the factory does so
    with GitHub's title rather than a local one the sync is about to overwrite.
    """
    result = maybe_sync(store, card, transport=transport)
    if result is None:
        return card
    _record_sync(ctx, card.card_key, result)
    return store.get(card.card_key) or card


def _record_sync(ctx: AuditContext, card_key: str, result: dict[str, object]) -> None:
    """Audit one GitHub sync. Carries ``ok`` from the result — a failed sync is a
    visible ``ok=False`` entry on the chain, not a gap in it."""
    ctx.audit.record(
        actor=ctx.actor,
        kind="sync_card_github",
        correlation_key=card_key,
        target_service="github",
        endpoint=f"{ctx.endpoint}/{card_key}",
        status_code=int(HTTPStatus.OK) if result.get("ok") else 0,
        ok=bool(result.get("ok", False)),
    )


def list_cards(
    store: CardStore,
    *,
    status: str | None = None,
    milestone: str | None = None,
    assignee: str | None = None,
    tier: str | None = None,
) -> dict[str, object]:
    """The backlog: this tenant's cards, highest priority first, optionally
    narrowed to a board column / release / owner / difficulty tier."""
    cards = store.list(status=status, milestone=milestone, assignee=assignee, tier=tier)
    return {"count": len(cards), "cards": [c.model_dump(mode="json") for c in cards]}


def get_card(store: CardStore, card_key: str) -> Card:
    card = store.get(card_key)
    if card is None:
        raise CardNotFoundError(card_key)
    return card


def create_card(
    store: CardStore,
    ctx: AuditContext,
    data: CardCreate,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Card:
    """Create a card, audit it, and run the intake hook.

    A card created straight into ``ready`` with a tier is dispatched into the
    factory exactly as a promotion would be (RFC-0019 §3.2) — the intake trigger
    is the card's state, not which verb (or which transport) produced it.

    Propagates :class:`~cfactory.cards.DuplicateCardKeyError` when the key is
    taken.
    """
    card = store.create(data)
    _record(ctx, kind="create_card", card_key=card.card_key, status_code=int(HTTPStatus.CREATED))
    return _intake(store, ctx, _github(store, ctx, card, transport), transport)


def update_card(
    store: CardStore,
    ctx: AuditContext,
    card_key: str,
    changes: CardUpdate,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Card:
    """Apply a partial update, audit it, and run the intake hook.

    This is also how a card MOVES between board columns (``status``) and how it
    is REPRIORITISED (``priority``) — there is deliberately no separate store
    path for either. Only fields actually set on ``changes`` are applied.

    Promoting a card to ``ready`` with a tier is the RFC-0019 §3.2 intake
    trigger: it dispatches into the factory and comes back joined to a work item
    (``correlation_key`` set, status ``in_progress``). Re-promoting an
    already-joined card does NOT dispatch again.
    """
    card = store.update(card_key, changes.model_dump(exclude_unset=True))
    if card is None:
        raise CardNotFoundError(card_key)
    _record(ctx, kind="update_card", card_key=card_key, status_code=int(HTTPStatus.OK))
    return _intake(store, ctx, _github(store, ctx, card, transport), transport)


def sync_card_github(
    store: CardStore,
    ctx: AuditContext,
    card_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Sync one card with its GitHub issue on demand (RFC-0019 §3.5).

    Opens the issue if the card has none, adopts and mirrors it if it has —
    which is also how a card picks up an issue that was closed, renamed or
    relabelled on github.com since the last sync. **GitHub wins** on every
    mirrored field; the card's planning fields are untouched. See
    :mod:`cfactory.github_sync` for the full rule.

    Returns the sync result AND the resulting card, because the two answer
    different questions: whether the call reached GitHub, and what the board now
    says. A failed sync is a 200 with ``ok=False`` and the reason on the card —
    an unreachable GitHub is not a board error.
    """
    card = get_card(store, card_key)
    result = sync_card(store, card, transport=transport)
    _record_sync(ctx, card_key, result)
    return {"sync": result, "card": (store.get(card_key) or card).model_dump(mode="json")}


def _finish_stage_action(
    store: CardStore, ctx: AuditContext, card_key: str, result: dict[str, Any]
) -> dict[str, object]:
    """Turn a stage-action result into the response both surfaces return.

    A REFUSAL becomes :class:`StageRefusedError` (409 over REST, an error payload
    over MCP) — the card was not touched, so there is nothing to audit. Anything
    that reached (or deliberately skipped) an upstream is audited and answered
    with both halves: what the dispatch did, and what the board now says.
    """
    refused = result.get("refused")
    if refused:
        raise StageRefusedError(str(refused), str(result.get("reason", "")))
    record_dispatch(ctx, card_key, result)
    card = store.get(card_key)
    return {"stage": result, "card": card.model_dump(mode="json") if card else None}


def run_card_stage(
    store: CardStore,
    ctx: AuditContext,
    card_key: str,
    stage: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Push one card into ONE named stage — ``plan``, ``code`` or ``test``.

    Deliberately overrides tier routing for the DESTINATION (a ``low`` card can be
    planned, a ``hard`` one built straight away) while the tier still supplies the
    payload. Refuses rather than dispatching into nothing: no tier, a stage already
    in flight, ``test`` with no completed build, or no configured intake project.
    Idempotent — a stage already recorded complete is skipped with a reason.
    """
    parsed = parse_stage(stage)
    if parsed is None:
        raise UnknownStageError(stage)
    card = get_card(store, card_key)
    result = dispatch_stage(store, card, parsed, transport=transport)
    return _finish_stage_action(store, ctx, card_key, result)


def run_card_sequence(
    store: CardStore,
    ctx: AuditContext,
    card_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Push one card through plan -> code -> test.

    Only the FIRST stage still owed is dispatched now; each later one goes out when
    the previous reaches terminal success on the work-item timeline (see
    ``card_intake.apply_status``), so there is no second orchestrator to keep in
    step. Stages already complete are skipped, so this resumes a part-finished or
    failed card instead of restarting it.
    """
    card = get_card(store, card_key)
    result = start_sequence(store, card, transport=transport)
    return _finish_stage_action(store, ctx, card_key, result)


def import_cards(
    store: CardStore,
    ctx: AuditContext,
    *,
    project: str | None = None,
    full: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Import the configured repository's EXISTING issues as cards (RFC-0020 §3.6).

    The other direction from :func:`sync_card_github`: that one takes a card and
    finds it an issue, this one takes a repository and fills the board. Runs
    incrementally once a watermark exists (``full=True`` re-reads everything);
    both are safe to repeat, because the import is an upsert against the unique
    ``(tenant_id, issue_ref)`` index rather than an insert.

    Imported cards land in ``backlog`` (or ``done`` if the issue is closed) and
    **never** in ``ready`` — see :mod:`cfactory.issue_import`.

    Audited like every other card mutation, with ``ok`` carried from the result:
    an unreachable provider is a visible ``ok=False`` entry on the chain, not a
    gap in it and not a 500.
    """
    result = import_issues(store, project=project, full=full, transport=transport)
    ctx.audit.record(
        actor=ctx.actor,
        kind="import_cards",
        correlation_key=str(result.get("project") or "-"),
        target_service="git_provider",
        endpoint=f"{ctx.endpoint}/import",
        status_code=int(HTTPStatus.OK) if result.get("ok") else 0,
        ok=bool(result.get("ok", False)),
    )
    return result


def delete_card(store: CardStore, ctx: AuditContext, card_key: str) -> dict[str, object]:
    if not store.delete(card_key):
        raise CardNotFoundError(card_key)
    _record(ctx, kind="delete_card", card_key=card_key, status_code=int(HTTPStatus.OK))
    return {"card_key": card_key, "deleted": True}

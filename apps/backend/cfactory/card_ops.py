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

from dataclasses import dataclass
from http import HTTPStatus

import httpx

from .audit import AuditStore
from .card_intake import maybe_dispatch
from .cards import Card, CardCreate, CardStore, CardUpdate

# Audit rows carry a ``target_service``; a card mutation never leaves CFactory,
# so it is attributed to CFactory itself rather than to an upstream factory.
_SELF = "cfactory"

# Audit ``endpoint`` prefix for a mutation that arrived over REST. The MCP
# transport passes its own so the trail says which surface it came in through.
REST_ENDPOINT = "/api/cards"


class CardNotFoundError(Exception):
    """No card with that key exists *in the caller's tenant scope*."""


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
    ctx.audit.record(
        actor=ctx.actor,
        kind="dispatch_card",
        correlation_key=str(result.get("correlation_key") or card.card_key),
        target_service=str(result.get("target_service", _SELF)),
        endpoint=f"{ctx.endpoint}/{card.card_key}",
        status_code=int(result.get("status_code", 0)),
        ok=bool(result.get("ok", False)),
    )
    return store.get(card.card_key) or card


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
    return _intake(store, ctx, card, transport)


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
    return _intake(store, ctx, card, transport)


def delete_card(store: CardStore, ctx: AuditContext, card_key: str) -> dict[str, object]:
    if not store.delete(card_key):
        raise CardNotFoundError(card_key)
    _record(ctx, kind="delete_card", card_key=card_key, status_code=int(HTTPStatus.OK))
    return {"card_key": card_key, "deleted": True}

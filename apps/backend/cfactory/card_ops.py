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
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from .audit import AuditStore
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


def create_card(store: CardStore, ctx: AuditContext, data: CardCreate) -> Card:
    """Create a card and audit it. Propagates
    :class:`~cfactory.cards.DuplicateCardKeyError` when the key is taken."""
    card = store.create(data)
    _record(ctx, kind="create_card", card_key=card.card_key, status_code=int(HTTPStatus.CREATED))
    return card


def update_card(store: CardStore, ctx: AuditContext, card_key: str, changes: CardUpdate) -> Card:
    """Apply a partial update and audit it.

    This is also how a card MOVES between board columns (``status``) and how it
    is REPRIORITISED (``priority``) — there is deliberately no separate store
    path for either. Only fields actually set on ``changes`` are applied.
    """
    card = store.update(card_key, changes.model_dump(exclude_unset=True))
    if card is None:
        raise CardNotFoundError(card_key)
    _record(ctx, kind="update_card", card_key=card_key, status_code=int(HTTPStatus.OK))
    return card


def delete_card(store: CardStore, ctx: AuditContext, card_key: str) -> dict[str, object]:
    if not store.delete(card_key):
        raise CardNotFoundError(card_key)
    _record(ctx, kind="delete_card", card_key=card_key, status_code=int(HTTPStatus.OK))
    return {"card_key": card_key, "deleted": True}

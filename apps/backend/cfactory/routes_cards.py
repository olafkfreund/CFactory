"""Planning-card CRUD — the read/write half of the control plane (RFC-0019 §3.1).

Reuses the cockpit's existing machinery rather than inventing a parallel one:
``require_scope("read"/"write")`` for authorization, ``cards_store_dep`` for the
tenant-scoped store, ``identity_dep`` for the actor, and ``audit_dep`` for the
tamper-evident HMAC chain — EVERY mutation here appends an audit entry, same as
``/api/actions/execute``.

There is no separate move/reprioritise endpoint: a move is
``PATCH {"status": ...}`` and a reprioritise is ``PATCH {"priority": ...}``.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .api_deps import audit_dep, cards_store_dep
from .audit import AuditStore
from .auth import require_scope
from .cards import Card, CardCreate, CardStore, CardTier, CardUpdate, DuplicateCardKeyError
from .cards import CardStatus as CardStatusT
from .enterprise import identity_dep

router = APIRouter(tags=["cards"])

# Audit rows carry a ``target_service``; a card mutation never leaves CFactory,
# so it is attributed to CFactory itself rather than to an upstream factory.
_SELF = "cfactory"


def _record(
    audit: AuditStore,
    *,
    actor: str,
    kind: str,
    card_key: str,
    status_code: int,
) -> None:
    """Append a card mutation to the shared HITL audit chain.

    The chain's ``correlation_key`` column is the entity key of the thing that
    was mutated; for a card that is its ``card_key`` (which is *not* yet an
    RFC-0001 correlation key — a planned card has none until it enters the
    factory).
    """
    audit.record(
        actor=actor,
        kind=kind,
        correlation_key=card_key,
        target_service=_SELF,
        endpoint=f"/api/cards/{card_key}",
        status_code=status_code,
        ok=True,
    )


@router.get("/api/cards")
def list_cards(
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
    status: CardStatusT | None = None,
    milestone: str | None = None,
    assignee: str | None = None,
    tier: CardTier | None = None,
) -> dict[str, object]:
    """The backlog: this tenant's cards, highest priority first.

    Optional filters narrow to a board column (``status``), a release
    (``milestone``), an owner (``assignee``, human or factory runtime), or a
    difficulty tier.
    """
    cards = store.list(status=status, milestone=milestone, assignee=assignee, tier=tier)
    return {"count": len(cards), "cards": [c.model_dump(mode="json") for c in cards]}


@router.post("/api/cards", status_code=HTTPStatus.CREATED)
def create_card(
    req: CardCreate,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> Card:
    """Create a card. Omit ``card_key`` to have the next ``FCT-<n>`` assigned.

    409 if the tenant already holds that key."""
    try:
        card = store.create(req)
    except DuplicateCardKeyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT, detail=f"card already exists: {exc.args[0]!r}"
        ) from None
    _record(
        audit,
        actor=actor,
        kind="create_card",
        card_key=card.card_key,
        status_code=int(HTTPStatus.CREATED),
    )
    return card


@router.get("/api/cards/{card_key}")
def get_card(
    card_key: str,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
) -> Card:
    card = store.get(card_key)
    if card is None:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"no card {card_key!r}")
    return card


@router.patch("/api/cards/{card_key}")
def update_card(
    card_key: str,
    req: CardUpdate,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> Card:
    """Update any mutable field. This is also how a card MOVES between board
    columns (``status``) and how it is REPRIORITISED (``priority``).

    Only fields present in the body are applied; an explicit ``null`` clears a
    nullable field."""
    card = store.update(card_key, req.model_dump(exclude_unset=True))
    if card is None:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"no card {card_key!r}")
    _record(
        audit, actor=actor, kind="update_card", card_key=card_key, status_code=int(HTTPStatus.OK)
    )
    return card


@router.delete("/api/cards/{card_key}")
def delete_card(
    card_key: str,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    if not store.delete(card_key):
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"no card {card_key!r}")
    _record(
        audit, actor=actor, kind="delete_card", card_key=card_key, status_code=int(HTTPStatus.OK)
    )
    return {"card_key": card_key, "deleted": True}

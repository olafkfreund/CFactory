"""Planning-card CRUD — the read/write half of the control plane (RFC-0019 §3.1).

Reuses the cockpit's existing machinery rather than inventing a parallel one:
``require_scope("read"/"write")`` for authorization, ``cards_store_dep`` for the
tenant-scoped store, ``identity_dep`` for the actor, and ``audit_dep`` for the
tamper-evident HMAC chain — EVERY mutation here appends an audit entry, same as
``/api/actions/execute``.

There is no separate move/reprioritise endpoint: a move is
``PATCH {"status": ...}`` and a reprioritise is ``PATCH {"priority": ...}``.

The operations themselves live in :mod:`cfactory.card_ops`, shared byte-for-byte
with the MCP board tools (RFC-0019 §3.3 — programmatic equivalence has to be a
property of one implementation, not a coincidence between two). What is left
here is purely HTTP: dependency wiring and mapping the domain errors onto status
codes.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from . import card_ops
from .api_deps import audit_dep, cards_store_dep
from .audit import AuditStore
from .auth import require_scope
from .card_ops import AuditContext, CardNotFoundError
from .cards import Card, CardCreate, CardStore, CardTier, CardUpdate, DuplicateCardKeyError
from .cards import CardStatus as CardStatusT
from .enterprise import identity_dep

router = APIRouter(tags=["cards"])


def _not_found(card_key: str) -> HTTPException:
    return HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"no card {card_key!r}")


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
    return card_ops.list_cards(
        store, status=status, milestone=milestone, assignee=assignee, tier=tier
    )


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
        return card_ops.create_card(store, AuditContext(audit, actor), req)
    except DuplicateCardKeyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT, detail=f"card already exists: {exc.args[0]!r}"
        ) from None


@router.get("/api/cards/{card_key}")
def get_card(
    card_key: str,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
) -> Card:
    try:
        return card_ops.get_card(store, card_key)
    except CardNotFoundError:
        raise _not_found(card_key) from None


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
    try:
        return card_ops.update_card(store, AuditContext(audit, actor), card_key, req)
    except CardNotFoundError:
        raise _not_found(card_key) from None


@router.delete("/api/cards/{card_key}")
def delete_card(
    card_key: str,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    try:
        return card_ops.delete_card(store, AuditContext(audit, actor), card_key)
    except CardNotFoundError:
        raise _not_found(card_key) from None

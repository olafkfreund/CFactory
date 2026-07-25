"""Planning-card CRUD — the read/write half of the control plane (RFC-0019 §3.1).

Reuses the cockpit's existing machinery rather than inventing a parallel one:
``require_scope("read"/"write")`` for authorization, ``cards_store_dep`` for the
tenant-scoped store, ``identity_dep`` for the actor, and ``audit_dep`` for the
tamper-evident HMAC chain — EVERY mutation here appends an audit entry, same as
``/api/actions/execute``.

There is no separate move/reprioritise endpoint: a move is
``PATCH {"status": ...}`` and a reprioritise is ``PATCH {"priority": ...}``.

The operations themselves live in :mod:`cfactory.card_ops`, shared with the MCP
board tools (RFC-0019 §3.3 — programmatic equivalence has to be a property of
one implementation, not a coincidence between two). That includes the §3.2
intake dispatch, so there is no separate hook to call here: a write that leaves
a card ``ready`` with a tier enters it into the factory whichever surface made
the write. What is left in this module is purely HTTP — dependency wiring and
mapping the domain errors onto status codes.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException

from . import card_ops
from .api_deps import action_transport_dep, audit_dep, cards_store_dep
from .audit import AuditStore
from .auth import require_scope
from .card_ops import AuditContext, CardNotFoundError
from .cards import (
    Card,
    CardCreate,
    CardStore,
    CardTier,
    CardUpdate,
    DuplicateCardKeyError,
    DuplicateIssueRefError,
)
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
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> Card:
    """Create a card. Omit ``card_key`` to have the next ``FCT-<n>`` assigned.

    A card created straight into ``ready`` with a tier is dispatched into the
    factory exactly as a promotion would be (RFC-0019 §3.2) — the intake trigger
    is the card's state, not which verb produced it.

    409 if the tenant already holds that key."""
    try:
        return card_ops.create_card(store, AuditContext(audit, actor), req, transport=transport)
    except DuplicateCardKeyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT, detail=f"card already exists: {exc.args[0]!r}"
        ) from None
    except DuplicateIssueRefError as exc:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=f"another card already tracks issue {exc.args[0]!r}",
        ) from None


@router.post("/api/cards/import")
def import_cards(  # noqa: PLR0913 — a FastAPI signature IS the DI surface; see
    # update_card below. The injected seams are wiring, not a call-site argument
    # list, so splitting them would only hide it.
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
    project: str | None = None,
    full: bool = False,
) -> dict[str, object]:
    """Import the repository's EXISTING issues into the backlog (RFC-0020 §3.6).

    Connecting a repo should bring the work with it. This lists the configured
    project's issues through the provider protocol (so it works on GitLab and
    Azure DevOps too) and upserts each one as a card.

    Imported cards land in `backlog` — a closed issue in `done` — and **never**
    in `ready`: `ready` + a tier is the dispatch trigger, and a repo full of
    `factory:low` issues would otherwise fire a build per issue.

    Idempotent: re-running updates the same cards rather than duplicating them.
    Incremental after the first run (`full=true` re-reads everything). Pull
    requests are never imported.

    **Not live.** There is no webhook receiver, so this is a poll: an issue filed
    since the last run appears on the next one. The result carries
    `last_synced_at`, and `truncated` when `CFACTORY_IMPORT_MAX` capped the run.

    A provider outage returns 200 with `ok == false` and the reason, rather than
    failing the board.
    """
    return card_ops.import_cards(
        store, AuditContext(audit, actor), project=project, full=full, transport=transport
    )


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
def update_card(  # noqa: PLR0913 — a FastAPI signature IS the DI surface; the
    # params are injected seams (store/audit/transport/scope/actor), not a
    # call-site argument list, so splitting them would only hide the wiring.
    card_key: str,
    req: CardUpdate,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> Card:
    """Update any mutable field. This is also how a card MOVES between board
    columns (``status``) and how it is REPRIORITISED (``priority``).

    Promoting a card to ``ready`` with a tier is the RFC-0019 §3.2 intake
    trigger: it dispatches into the factory and comes back joined to a work item
    (``correlation_key`` set, status ``in_progress``). Re-promoting an
    already-joined card does NOT dispatch again.

    Only fields present in the body are applied; an explicit ``null`` clears a
    nullable field."""
    try:
        return card_ops.update_card(
            store, AuditContext(audit, actor), card_key, req, transport=transport
        )
    except CardNotFoundError:
        raise _not_found(card_key) from None
    except DuplicateIssueRefError as exc:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=f"another card already tracks issue {exc.args[0]!r}",
        ) from None


@router.post("/api/cards/{card_key}/sync-github")
def sync_card_github(
    card_key: str,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Sync this card with its GitHub issue (RFC-0019 §3.5).

    Opens an issue in `CFACTORY_GITHUB_REPO` if the card has none; otherwise
    adopts the issue named by `issue_ref` and mirrors it down. **GitHub is the
    record of truth: on conflict its title / labels / open-closed state
    overwrite the card's.** The card's planning fields (priority, tier,
    milestone, acceptance criteria) are never touched.

    Idempotent — syncing twice adopts, it does not open a second issue. A GitHub
    outage returns 200 with `sync.ok == false` and the reason recorded on the
    card, rather than failing the board.
    """
    try:
        return card_ops.sync_card_github(
            store, AuditContext(audit, actor), card_key, transport=transport
        )
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

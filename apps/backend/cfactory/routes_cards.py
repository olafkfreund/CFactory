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

from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException

from . import card_ops
from .api_deps import action_transport_dep, audit_dep, cards_store_dep
from .audit import AuditStore
from .auth import require_scope
from .card_ops import AuditContext, CardNotFoundError, StageRefusedError, UnknownStageError
from .cards import Card, CardCreate, CardStore, CardTier, CardUpdate, DuplicateCardKeyError
from .cards import CardStatus as CardStatusT
from .enterprise import identity_dep

router = APIRouter(tags=["cards"])


def _not_found(card_key: str) -> HTTPException:
    return HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"no card {card_key!r}")


def _stage_action(
    store: CardStore,
    ctx: AuditContext,
    card_key: str,
    transport: httpx.BaseTransport | None,
    stage: str | None,
) -> dict[str, object]:
    """Run one stage action (or the whole sequence when ``stage`` is None).

    The four routes below differ only in which stage they name, so the error
    mapping lives here once: 404 for an unknown card, and 409 for a REFUSED
    transition carrying both a machine-readable ``reason`` and a human
    ``message`` — never a dispatch into nothing reported as success.
    """
    try:
        if stage is None:
            return card_ops.run_card_sequence(store, ctx, card_key, transport=transport)
        return card_ops.run_card_stage(store, ctx, card_key, stage, transport=transport)
    except CardNotFoundError:
        raise _not_found(card_key) from None
    except StageRefusedError as exc:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={"reason": exc.code, "message": exc.message},
        ) from None
    except UnknownStageError as exc:  # pragma: no cover — the paths are literals
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=f"unknown stage {exc.args[0]!r}"
        ) from None


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


@dataclass
class StageActionDeps:
    """The injected seams every stage action needs, bundled as one dependency.

    The four routes below are identical apart from which stage they name, so the
    wiring is declared once here instead of as four copies of the same five-param
    FastAPI signature. Requires the WRITE scope, exactly as the other mutations do.
    """

    store: Annotated[CardStore, Depends(cards_store_dep)]
    audit: Annotated[AuditStore, Depends(audit_dep)]
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)]
    _scope: Annotated[str | None, Depends(require_scope("write"))]
    actor: Annotated[str, Depends(identity_dep)]

    def run(self, card_key: str, stage: str | None) -> dict[str, object]:
        return _stage_action(
            self.store, AuditContext(self.audit, self.actor), card_key, self.transport, stage
        )


@router.post("/api/cards/{card_key}/actions/plan")
def plan_card(card_key: str, deps: Annotated[StageActionDeps, Depends()]) -> dict[str, object]:
    """Send this card to PFactory for planning (RFC-0020 §3.7).

    Explicit, so it OVERRIDES tier routing for the destination — a `low` card can
    be planned even though its tier would skip planning — while the tier still
    supplies the payload. 409 with a `reason` code when the transition is
    impossible; a stage already complete is skipped with a reason, not re-run.
    """
    return deps.run(card_key, "plan")


@router.post("/api/cards/{card_key}/actions/code")
def code_card(card_key: str, deps: Annotated[StageActionDeps, Depends()]) -> dict[str, object]:
    """Send this card to AIFactory to be built (RFC-0020 §3.7).

    Allowed on a `hard` card that was never planned — the response warns that a
    stage was skipped rather than refusing, because building without a plan is a
    lossy choice a human is entitled to make. 409 when there is no tier to build
    from or no configured intake project.
    """
    return deps.run(card_key, "code")


@router.post("/api/cards/{card_key}/actions/test")
def test_card(card_key: str, deps: Annotated[StageActionDeps, Depends()]) -> dict[str, object]:
    """Send this card to TFactory to be verified (RFC-0020 §3.7).

    Refuses with `no_build_to_verify` unless the card is joined to a work item AND
    its code stage completed: asking to verify something that was never built would
    generate verification lanes against nothing.
    """
    return deps.run(card_key, "test")


@router.post("/api/cards/{card_key}/actions/run")
def run_card(card_key: str, deps: Annotated[StageActionDeps, Depends()]) -> dict[str, object]:
    """Run this card through plan -> code -> test (RFC-0020 §3.7).

    Only the first stage still owed is dispatched now; each later one goes out when
    the previous reaches terminal success on the work-item timeline. Stages already
    complete are skipped, so this RESUMES a part-finished or failed card rather than
    restarting it, and a failed stage stops the sequence with the card blocked.
    """
    return deps.run(card_key, None)


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

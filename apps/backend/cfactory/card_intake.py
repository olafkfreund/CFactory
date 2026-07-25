"""Card -> factory intake, and PARR status write-back (RFC-0019 §3.2).

Two halves of one loop:

**Out.** Promoting a card to ``ready`` *with a tier* makes it a first-class
intake source alongside a labelled GitHub issue (RFC-0011). The dispatch reuses
the cockpit's existing confirmed-write path verbatim — a
:class:`~cfactory.actions.PreparedAction` executed by
:func:`~cfactory.actions.execute_action` — so it inherits the SSRF endpoint
guard, the upstream bearer token, and the never-raises contract. Tier routing is
RFC-0011 §3: ``low``/``medium`` take the skip-planning fast path straight into an
AIFactory build; ``hard`` routes through PFactory planning first.

**Back.** The card's ``correlation_key`` is set from the dispatch response, which
joins it to the work item. From then on the SAME completion-event stream that
threads the work-item timeline also writes the card's status, so the board is
the live view rather than a stale copy.

Idempotency has no new column: ``correlation_key`` non-NULL *is* "already in the
factory", so a re-promotion is a no-op. A dispatch that fails moves the card to
``blocked`` — never left sitting in ``ready`` as though it had been dispatched.
"""

from __future__ import annotations

from typing import Any

import httpx

from .actions import PreparedAction, execute_action
from .cards import Card, CardStore
from .config import Settings, get_settings
from .models import Service, WorkItem
from .status_taxonomy import is_done, is_failure_or_stuck

# The two intake doors, verified against the fleet's own driver docs
# (Factory docs/benchmarks/pipeline-driver-design.md, "Intake: three doors").
# ``from-issue`` accepts a pre-fetched ``payload`` so no GitHub issue is needed —
# which is exactly a card. ``from-plan`` is not usable here: it takes a signed
# contract, which a planning card does not have.
AIFACTORY_INTAKE_ENDPOINT = "/api/tasks/from-issue"
PFACTORY_INTAKE_ENDPOINT = "/api/plan/sessions/ingest-text"

# RFC-0011 §3 (normative): `hard` is the only tier that gets PFactory's full
# decomposition; low/medium skip planning.
_PLANNING_TIERS = {"hard"}

# Stages in pipeline order, furthest-along first — the same ordering
# ``actions._review_target`` uses to find the stage actually in flight.
_STAGE_ORDER = ("tfactory", "aifactory", "pfactory")


def _brief(card: Card) -> str:
    """The card rendered as the markdown body both intake doors accept."""
    lines = [f"# {card.title}"]
    if card.acceptance_criteria:
        lines += ["", "## Acceptance Criteria", ""]
        lines += [f"- {ac}" for ac in card.acceptance_criteria]
    return "\n".join(lines)


def prepare_dispatch(card: Card, *, settings: Settings) -> PreparedAction | None:
    """Build the not-yet-executed intake write for a ready card.

    Returns ``None`` only when a build dispatch is impossible because no intake
    project is configured — AIFactory's ``from-issue`` requires a ``project_id``
    and the card contract carries none, so it comes from
    ``CFACTORY_INTAKE_PROJECT_ID``. The caller turns that into a blocked card
    with a reason rather than a silent no-op.
    """
    if card.tier in _PLANNING_TIERS:
        return PreparedAction(
            kind="dispatch_card",
            correlation_key=card.card_key,
            target_service=Service.PFACTORY.value,
            method="POST",
            endpoint=PFACTORY_INTAKE_ENDPOINT,
            payload={
                "title": card.title,
                "category": "software",
                "channel": "cfactory",
                "text": _brief(card),
            },
            rationale=(
                f"Plan {card.card_key!r} in PFactory first — tier {card.tier!r} gets "
                "full decomposition (RFC-0011 §3) before any code is written."
            ),
        )
    if not settings.intake_project_id:
        return None
    return PreparedAction(
        kind="dispatch_card",
        correlation_key=card.card_key,
        target_service=Service.AIFACTORY.value,
        method="POST",
        endpoint=AIFACTORY_INTAKE_ENDPOINT,
        payload={
            "project_id": settings.intake_project_id,
            "payload": {
                "title": card.title,
                "body": _brief(card),
                # The tier label AIFactory's classifier reads (RFC-0011 §2).
                "labels": [f"factory:{card.tier}"],
            },
            "auto_continue": True,
        },
        rationale=(
            f"Build {card.card_key!r} in AIFactory — tier {card.tier!r} takes the "
            "skip-planning fast path (RFC-0011 §3)."
        ),
    )


def _upstream_key(body: Any) -> str | None:
    """The correlation key to join the card on, out of an intake response.

    Neither door returns an RFC-0001 correlation key directly: with no GitHub
    issue behind the card, both adapters key the work item on the upstream's own
    id (``adapters/aifactory.py`` and ``adapters/pfactory.py`` both fall back to
    ``task_id``), so that id IS the correlation key CFactory will see.
    """
    if not isinstance(body, dict):
        return None
    for key in ("correlation_key", "task_id", "session_id", "id", "spec_id"):
        value = body.get(key)
        if value:
            return str(value)
    return None


def dispatch_card(
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Send a ready card into the factory. Never raises.

    Idempotent: a card that already carries a ``correlation_key`` is in the
    factory, so this is a no-op rather than a second build.
    """
    settings = settings or get_settings()
    if card.correlation_key:
        return {
            "dispatched": False,
            "ok": True,
            "reason": "already dispatched",
            "correlation_key": card.correlation_key,
        }

    action = prepare_dispatch(card, settings=settings)
    if action is None:
        store.update(card.card_key, {"status": "blocked"})
        return {
            "dispatched": False,
            "ok": False,
            "status_code": 0,
            "reason": (
                "no intake project configured — set CFACTORY_INTAKE_PROJECT_ID to "
                "dispatch a low/medium card to AIFactory"
            ),
        }

    result = execute_action(action, settings=settings, transport=transport)
    if not result.get("ok"):
        # Fail-safe and truthful: a card that could NOT enter the factory must
        # not sit in `ready` looking dispatched. It is blocked, and the audit
        # entry the route writes carries the upstream status.
        store.update(card.card_key, {"status": "blocked"})
        return {"dispatched": False, "target_service": action.target_service, **result}

    # No key in the response would leave the card unjoinable, so fall back to the
    # card's own stable id — the same string we sent upstream as the title.
    correlation_key = _upstream_key(result.get("body")) or card.card_key
    store.update(card.card_key, {"correlation_key": correlation_key, "status": "in_progress"})
    return {
        "dispatched": True,
        "target_service": action.target_service,
        "correlation_key": correlation_key,
        **result,
    }


def maybe_dispatch(
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any] | None:
    """Intake hook for the card write routes.

    RFC-0019 §3.2: intake is "a card in ``ready`` status WITH a difficulty tier".
    A ready card with no tier is a legitimate board state (queued for triage), so
    it is left alone — ``None`` means "not an intake event".
    """
    if card.status != "ready" or not card.tier:
        return None
    return dispatch_card(store, card, settings=settings, transport=transport)


def card_status_for(item: WorkItem) -> str | None:
    """Map a work item's live PARR state onto the card status enum.

    The card contract has five columns and gains none here, so PARR's richer
    lifecycle (planned -> building -> verifying -> verdict) collapses onto them
    honestly: anything in flight is ``in_progress``, a failed or stuck stage is
    ``blocked``, and only a *verdict* is ``done``. A finished PLAN is not a
    verdict — the build comes next — so a done PFactory stage stays
    ``in_progress``. ``None`` when the item carries no stage state at all.
    """
    for attr in _STAGE_ORDER:
        status = getattr(item, attr).status
        if not status:
            continue
        if is_failure_or_stuck(status):
            return "blocked"
        if is_done(status):
            return "in_progress" if attr == "pfactory" else "done"
        return "in_progress"
    return None


def apply_status(cards: CardStore, item: WorkItem) -> Card | None:
    """Write a work item's live state back onto the card joined to it.

    Called from every completion-event ingress after the work item is updated.
    A no-op when no card is joined to this correlation key (the ordinary
    GitHub-issue case) or when the mapped status is unchanged.
    """
    card = cards.get_by_correlation_key(item.correlation_key)
    if card is None:
        return None
    status = card_status_for(item)
    if status is None or status == card.status:
        return None
    return cards.update(card.card_key, {"status": status})

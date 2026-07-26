"""Card -> factory intake, explicit stage actions, and PARR status write-back.

Two halves of one loop (RFC-0019 §3.2):

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

Explicit stage actions (RFC-0020 §3.7)
--------------------------------------
Tier routing decides where an *implicit* promotion goes. A human on the board can
instead say plan / code / test outright, or ``run`` the whole sequence — which
means an explicit action may send a ``low`` card to PFactory or a ``hard`` card
straight to a build. That override is deliberate; the tier still supplies the
PAYLOAD (planning depth, the ``factory:<tier>`` label AIFactory's classifier
reads), which is why a card with no tier is refused rather than guessed at.

Idempotency moved, and had to. It used to be "``correlation_key`` non-NULL means
already in the factory", but planning SETS that key, so under the old rule every
stage after the first no-oped and a sequence was impossible. The key now means
"the key this card's work is threaded on — reuse it", and the narrower question
("has THIS stage run?") is answered by the per-stage dispatch record in
``cards.stage_runs``. The write-once property is kept: the key is set exactly
once, and a later stage reuses it rather than re-pointing it.

A dispatch that fails moves the card to ``blocked`` with the reason recorded —
never left sitting in ``ready`` as though it had been dispatched, never a 500.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .actions import PreparedAction, execute_action
from .cards import Card, CardStore
from .config import Settings, get_settings
from .git_config import qualify_repo
from .models import Service, Stage, WorkItem
from .status_taxonomy import is_done, is_failure_or_stuck

# The three intake doors. plan/code are verified against the fleet's own driver
# docs (Factory docs/benchmarks/pipeline-driver-design.md, "Intake: three
# doors"): ``from-issue`` accepts a pre-fetched ``payload`` so no GitHub issue is
# needed — which is exactly a card — while ``from-plan`` is not usable here, it
# takes a signed contract a planning card does not have.
#
# The test door is TFactory's generic spec-ingestion route, read from the
# TFactory source at ``apps/web-server/server/routes/specs.py`` ("the portal 'New
# test from spec' door"): it takes ``{project_id, spec_id, spec_text}``, creates a
# verification task WITHOUT an AIFactory branch, and returns ``{"task_id": ...}``.
# It is the only TFactory door a card can use — ``/api/tfactory/tasks`` is
# read-only and ``/api/handback/aifactory-complete`` is AIFactory's re-test hook.
AIFACTORY_INTAKE_ENDPOINT = "/api/tasks/from-issue"
PFACTORY_INTAKE_ENDPOINT = "/api/plan/sessions/ingest-text"
TFACTORY_INTAKE_ENDPOINT = "/api/specs/ingest"

# RFC-0011 §3 (normative): `hard` is the only tier that gets PFactory's full
# decomposition; low/medium skip planning.
_PLANNING_TIERS = {"hard"}

# Work-item stage slices in pipeline order, furthest-along first — the same
# ordering ``actions._review_target`` uses to find the stage actually in flight.
_STAGE_ORDER = ("tfactory", "aifactory", "pfactory")

# ── Stage actions ──────────────────────────────────────────────────────────────

# The sequence ``run`` walks, and which service owns each stage. ``Stage`` is
# reused from cfactory.models rather than redeclared — it is already the fleet's
# plan/code/test vocabulary.
SEQUENCE: tuple[Stage, ...] = (Stage.PLAN, Stage.CODE, Stage.TEST)
STAGE_SERVICE: dict[Stage, Service] = {
    Stage.PLAN: Service.PFACTORY,
    Stage.CODE: Service.AIFACTORY,
    Stage.TEST: Service.TFACTORY,
}
# The same mapping read the other way, for turning a service name back into the
# stage it owns — which is what a completion event carries.
STAGE_FOR_SERVICE: dict[str, Stage] = {svc.value: stage for stage, svc in STAGE_SERVICE.items()}

# States a per-stage dispatch record can hold. ``queued`` is a stage a sequence
# has committed to but not yet sent; ``dispatched`` is in flight upstream;
# ``done``/``failed`` are terminal and settled from the work-item timeline.
QUEUED, DISPATCHED, DONE, FAILED = "queued", "dispatched", "done", "failed"
# Stages that still owe this card work. While any of these remain the card is
# NOT done, whatever an individual finished stage says (see card_status_for).
_PENDING = (QUEUED, DISPATCHED)


@dataclass(frozen=True)
class StageRefusal:
    """A stage action that must NOT be dispatched, and why.

    ``code`` is machine-readable (the REST route puts it on a 409, MCP returns
    it in the error payload); ``message`` is the sentence a human reads.
    """

    code: str
    message: str


def parse_stage(value: str) -> Stage | None:
    """The :class:`Stage` named by ``value``, or None if it is not a stage."""
    try:
        return Stage(value)
    except ValueError:
        return None


def _brief(card: Card) -> str:
    """The card rendered as the markdown body both intake doors accept."""
    lines = [f"# {card.title}"]
    if card.acceptance_criteria:
        lines += ["", "## Acceptance Criteria", ""]
        lines += [f"- {ac}" for ac in card.acceptance_criteria]
    return "\n".join(lines)


def default_stage(card: Card) -> Stage:
    """The stage tier routing would pick for this card (RFC-0011 §3).

    ``hard`` plans first; everything else builds directly. An explicit stage
    action may override this — :func:`stage_warnings` says when it did.
    """
    return Stage.PLAN if card.tier in _PLANNING_TIERS else Stage.CODE


def repo_ref(store: CardStore, settings: Settings, card: Card) -> str | None:
    """The provider-qualified repo reference this card's work targets (§3.5).

    Resolved through the SAME per-card target ``aifactory_project_id`` uses, so
    the host a card is dispatched against is the host its own repository is on —
    never a tenant-wide assumption, and never the receiving service's environment
    default. That last part is the bug this phase exists to fix: PFactory,
    AIFactory and TFactory each chose a git host from their own config, so a
    GitLab tenant's run reconnoitred github.com and opened a GitHub PR.

    ``None`` when the tenant has configured no project. The downstream services
    then behave exactly as they did before this phase, which for a deployment
    that never filled the panel in is the only honest answer available.
    """
    target = store.git_target_for_card(card, settings)
    return qualify_repo(target.provider, target.project)


def aifactory_project_id(
    store: CardStore, settings: Settings, card: Card | None = None
) -> str | None:
    """The AIFactory project a dispatched card is BUILT in.

    RFC-0020 §3.3: it comes from THIS CARD's repository — the one it names, or the
    one its issue lives in, or the tenant's default — which is editable in the
    cockpit, falling back for one release to the ``CFACTORY_INTAKE_PROJECT_ID`` env
    var that used to be its only source (see :mod:`cfactory.git_config`). This is
    the ONE place the value is read, so "which project does my build land in?" has
    exactly one answer, and since phase 8 that answer can differ per card: two
    cards on one board may build into two different AIFactory projects because
    they are for two different repositories.
    """
    if card is None:
        return store.git_target(settings).aifactory_project_id
    return store.git_target_for_card(card, settings).aifactory_project_id


def prepare_stage(
    card: Card, stage: Stage, *, project_id: str | None, repo: str | None = None
) -> PreparedAction | None:
    """Build the not-yet-executed intake write that sends ``card`` into ``stage``.

    Returns ``None`` only when the payload cannot be built because no intake
    project is configured — AIFactory's ``from-issue`` and TFactory's
    ``specs/ingest`` both require a ``project_id`` the card contract does not
    carry, so it comes from the tenant's git configuration
    (:func:`aifactory_project_id`). The caller turns that into a refusal
    (explicit action) or a blocked card (implicit promotion) rather than a silent
    no-op.

    ``repo`` is the provider-qualified reference from :func:`repo_ref`, and it is
    the ONLY thing RFC-0020 §3.5 adds to what goes out of here. It is sent on the
    existing ``repo`` field each door already has (TFactory's gained one in the
    same phase) rather than as a separate ``provider`` field, because two fields
    is two sources of truth and the one that got read would be whichever the
    receiving service happened to look at first. Omitted entirely when the tenant
    has configured no project, so an unconfigured deployment sends exactly the
    payload it sent before.
    """
    qualified = {"repo": repo} if repo else {}
    if stage is Stage.PLAN:
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
                **qualified,
            },
            rationale=(
                f"Plan {card.card_key!r} in PFactory — tier {card.tier!r} decides the "
                "decomposition depth (RFC-0011 §3); planning runs before any code."
            ),
        )
    if not project_id:
        return None
    if stage is Stage.TEST:
        return PreparedAction(
            kind="dispatch_card",
            correlation_key=card.card_key,
            target_service=Service.TFACTORY.value,
            method="POST",
            endpoint=TFACTORY_INTAKE_ENDPOINT,
            payload={
                "project_id": project_id,
                # TFactory keys the verification workspace on spec_id; the card's
                # own stable key is the natural one and is already a safe path
                # component ("FCT-42").
                "spec_id": card.card_key,
                "spec_text": _brief(card),
                **qualified,
            },
            rationale=(
                f"Verify {card.card_key!r} in TFactory — the acceptance criteria on the "
                "card become the spec the verification lanes are generated from."
            ),
        )
    return PreparedAction(
        kind="dispatch_card",
        correlation_key=card.card_key,
        target_service=Service.AIFACTORY.value,
        method="POST",
        endpoint=AIFACTORY_INTAKE_ENDPOINT,
        payload={
            "project_id": project_id,
            "payload": {
                "title": card.title,
                "body": _brief(card),
                # The tier label AIFactory's classifier reads (RFC-0011 §2).
                "labels": [f"factory:{card.tier}"],
            },
            "auto_continue": True,
            **qualified,
        },
        rationale=(
            f"Build {card.card_key!r} in AIFactory — tier {card.tier!r} supplies the "
            "classifier label and the model picks (RFC-0011 §2)."
        ),
    )


def prepare_dispatch(
    card: Card, *, project_id: str | None, repo: str | None = None
) -> PreparedAction | None:
    """The tier-routed intake write for an implicitly promoted ready card."""
    return prepare_stage(card, default_stage(card), project_id=project_id, repo=repo)


# ── Preconditions ─────────────────────────────────────────────────────────────
#
# The rules, in the order they are checked. Every one is a REFUSAL: the card is
# left exactly as it was and the caller is told why, because dispatching into
# nothing and reporting success is the failure this exists to prevent.
#
# 1. ``no_tier``            — the card has no difficulty tier. The tier supplies
#                             the payload (decomposition depth, the
#                             ``factory:<tier>`` label), so there is nothing to
#                             send. Same rule the implicit path applies.
# 2. ``stage_already_running`` — that stage has a live dispatch. Refusing is what
#                             makes a double-press one dispatch, not two.
# 3. ``no_build_to_verify`` — ``test`` on a card that has no ``correlation_key``
#                             or no COMPLETED code stage. There is no build to
#                             verify, so TFactory would generate lanes against
#                             nothing.
# 4. ``no_intake_project``  — ``code``/``test`` with no AIFactory project in the
#                             tenant's git config. Both doors require a
#                             project_id the card has not got.
#
# Deliberately NOT refusals — see :func:`stage_warnings`: coding a ``hard`` card
# with no plan (a skipped stage, warned about, still allowed), and any stage that
# overrides the tier's default destination (which is the point of the feature).


def _no_tier(card: Card) -> StageRefusal:
    """Rule 1, shared by the single-stage and the sequence entry points."""
    return StageRefusal(
        "no_tier",
        f"{card.card_key} has no difficulty tier, so there is no payload to build "
        "(the tier decides decomposition depth and the factory:<tier> label). "
        "Set a tier first.",
    )


def refuse_reason(card: Card, stage: Stage, *, project_id: str | None) -> StageRefusal | None:
    """The reason ``stage`` must not be dispatched for ``card``, or None."""
    if not card.tier:
        return _no_tier(card)
    running = [s.value for s in SEQUENCE if stage_status(card, s) == DISPATCHED]
    if stage.value in running:
        return StageRefusal(
            "stage_already_running",
            f"the {stage.value} stage of {card.card_key} is already running upstream; "
            "wait for it to finish rather than starting a second one.",
        )
    if stage is Stage.TEST and (not card.correlation_key or stage_status(card, Stage.CODE) != DONE):
        return StageRefusal(
            "no_build_to_verify",
            f"{card.card_key} has no completed build to verify — run the code stage to "
            "completion first, otherwise TFactory would generate lanes against nothing.",
        )
    if stage is not Stage.PLAN and not project_id:
        return StageRefusal(
            "no_intake_project",
            f"no AIFactory project configured, so the {stage.value} stage has no target — "
            "set it in Settings > Git integration.",
        )
    return None


def stage_warnings(card: Card, stage: Stage) -> list[str]:
    """Things the caller should know about an ALLOWED stage action.

    Neither is a reason to refuse: overriding tier routing is the whole point of
    an explicit action, and coding without a plan is a legitimate (if lossy)
    choice a human is entitled to make. Both are surfaced so it is a choice
    rather than a surprise.
    """
    warnings = []
    if (
        stage is Stage.CODE
        and card.tier in _PLANNING_TIERS
        and stage_status(card, Stage.PLAN) != DONE
    ):
        warnings.append(
            f"plan_skipped: tier {card.tier!r} would normally be decomposed by PFactory "
            "first; building straight from the card skips that stage."
        )
    if stage is not default_stage(card) and stage is not Stage.TEST:
        warnings.append(
            f"tier_override: tier {card.tier!r} routes to {default_stage(card).value!r} by "
            f"default; this action sends it to {stage.value!r} instead."
        )
    return warnings


# ── The per-stage dispatch record ─────────────────────────────────────────────
#
# ``cards.stage_runs`` maps a stage name to
# ``{service, status, dispatched_at, ref, detail}``. It is the ONLY idempotency
# guard for a stage: read it through :func:`stage_status` and nothing else, so
# there is one place that decides whether a stage may be sent.


def _runs(card: Card) -> dict[str, dict[str, Any]]:
    """The card's dispatch records, copied so callers cannot mutate the row."""
    return {k: dict(v) for k, v in (card.stage_runs or {}).items() if isinstance(v, dict)}


def stage_status(card: Card, stage: Stage) -> str | None:
    """This stage's recorded state (queued/dispatched/done/failed), or None."""
    return _runs(card).get(stage.value, {}).get("status")


def pending_stages(card: Card) -> list[str]:
    """Stages this card still owes: queued, or in flight upstream.

    While any remain the card is not finished, however green an individual
    completed stage looks — see :func:`card_status_for`.
    """
    return [s.value for s in SEQUENCE if stage_status(card, s) in _PENDING]


def _write_runs(
    store: CardStore, card: Card, updates: dict[str, dict[str, Any]], **card_fields: Any
) -> Card:
    """Merge ``updates`` into the card's dispatch records (plus any card fields)."""
    runs = _runs(card)
    for name, fields in updates.items():
        runs[name] = {**runs.get(name, {}), **fields}
    return store.update(card.card_key, {"stage_runs": runs, **card_fields}) or card


def _refused(refusal: StageRefusal, stage: Stage) -> dict[str, Any]:
    """The never-raises result shape for a refused stage action."""
    return {
        "dispatched": False,
        "ok": False,
        "stage": stage.value,
        "refused": refusal.code,
        "reason": refusal.message,
    }


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


def _send(
    store: CardStore,
    card: Card,
    action: PreparedAction,
    *,
    settings: Settings,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    """Execute one prepared stage write and record what happened. Never raises.

    The single place a stage's dispatch record is written, so the implicit
    tier-routed promotion and an explicit stage action cannot disagree about what
    "dispatched" means. The stage is read back off the action's target service
    rather than passed in again — one stage per service, so the action already
    says which stage it is.
    """
    stage = STAGE_FOR_SERVICE[action.target_service]
    result = execute_action(action, settings=settings, transport=transport)
    if not result.get("ok"):
        # Fail-safe and truthful: a card that could NOT enter the factory must
        # not sit in `ready` looking dispatched. It is blocked with the reason on
        # the record, and the audit entry the caller writes carries the upstream
        # status.
        reason = f"dispatch failed: HTTP {result.get('status_code', 0)}"
        _write_runs(
            store,
            card,
            {stage.value: {"service": action.target_service, "status": FAILED, "detail": reason}},
            status="blocked",
        )
        return {
            "dispatched": False,
            "stage": stage.value,
            "target_service": action.target_service,
            "reason": reason,
            **result,
        }

    ref = _upstream_key(result.get("body"))
    # Write-once: the FIRST stage to land sets the correlation key and every later
    # stage of the same card threads that same key rather than re-pointing it. No
    # key in the response at all would leave the card unjoinable, so fall back to
    # the card's own stable id.
    correlation_key = card.correlation_key or ref or card.card_key
    _write_runs(
        store,
        card,
        {
            stage.value: {
                "service": action.target_service,
                "status": DISPATCHED,
                "dispatched_at": datetime.now(UTC).isoformat(),
                "ref": ref,
                "detail": None,
            }
        },
        correlation_key=correlation_key,
        status="in_progress",
    )
    return {
        "dispatched": True,
        "stage": stage.value,
        "target_service": action.target_service,
        "correlation_key": correlation_key,
        **result,
    }


def dispatch_card(
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Send a ready card into the factory along its TIER's route. Never raises.

    Idempotent on the dispatch record of the stage the tier routes to, so
    re-promoting an already-dispatched card is a no-op exactly as before. (It used
    to key off ``correlation_key``; that could not survive a multi-stage sequence,
    since planning sets the key and every later stage would then no-op.)
    """
    settings = settings or get_settings()
    stage = default_stage(card)
    if stage_status(card, stage) in (DISPATCHED, DONE):
        return {
            "dispatched": False,
            "ok": True,
            "reason": "already dispatched",
            "stage": stage.value,
            "correlation_key": card.correlation_key,
        }

    action = prepare_dispatch(
        card,
        project_id=aifactory_project_id(store, settings, card),
        repo=repo_ref(store, settings, card),
    )
    if action is None:
        store.update(card.card_key, {"status": "blocked"})
        return {
            "dispatched": False,
            "ok": False,
            "status_code": 0,
            "reason": (
                "no AIFactory project configured — set one in Settings > Git integration "
                "to dispatch a low/medium card to AIFactory"
            ),
        }
    return _send(store, card, action, settings=settings, transport=transport)


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


def dispatch_stage(
    store: CardStore,
    card: Card,
    stage: Stage,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Send a card into ONE named stage, overriding tier routing. Never raises.

    Refuses (``ok=False`` with a ``refused`` code) rather than dispatching into
    nothing; no-ops with a reason on a stage already recorded complete, exactly as
    :func:`maybe_dispatch` no-ops on an already-dispatched card.
    """
    settings = settings or get_settings()
    if stage_status(card, stage) == DONE:
        return {
            "dispatched": False,
            "ok": True,
            "reason": f"the {stage.value} stage of {card.card_key} has already completed",
            "skipped": "stage_already_complete",
            "stage": stage.value,
            "correlation_key": card.correlation_key,
        }
    project_id = aifactory_project_id(store, settings, card)
    refusal = refuse_reason(card, stage, project_id=project_id)
    if refusal is not None:
        return _refused(refusal, stage)
    action = prepare_stage(card, stage, project_id=project_id, repo=repo_ref(store, settings, card))
    if action is None:  # refuse_reason already covers every buildable case
        return _refused(
            StageRefusal("no_intake_project", f"no payload can be built for {stage.value}"), stage
        )
    warnings = stage_warnings(card, stage)
    return {
        **_send(store, card, action, settings=settings, transport=transport),
        "warnings": warnings,
    }


def start_sequence(
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Queue plan -> code -> test and dispatch the first stage still owed.

    ``run`` is the full sequence whatever the tier, which for ``low``/``medium``
    deliberately overrides the skip-planning default; the tier still supplies the
    payload. Stages already recorded complete are SKIPPED rather than re-run, so
    ``run`` resumes a part-finished card instead of restarting it.
    """
    settings = settings or get_settings()
    if not card.tier:
        return _refused(_no_tier(card), SEQUENCE[0])
    live = next((s for s in SEQUENCE if stage_status(card, s) == DISPATCHED), None)
    if live is not None:
        return _refused(
            StageRefusal(
                "stage_already_running",
                f"the {live.value} stage of {card.card_key} is already running, so the "
                "sequence is already in motion; nothing to start.",
            ),
            live,
        )
    todo = [s for s in SEQUENCE if stage_status(card, s) != DONE]
    if not todo:
        return {
            "dispatched": False,
            "ok": True,
            "reason": f"every stage of {card.card_key} has already completed",
            "skipped": "sequence_already_complete",
            "sequence": [],
        }
    card = _write_runs(
        store,
        card,
        {
            s.value: {"service": STAGE_SERVICE[s].value, "status": QUEUED, "detail": None}
            for s in todo
        },
    )
    result = dispatch_stage(store, card, todo[0], settings=settings, transport=transport)
    return {"sequence": [s.value for s in todo], **result}


def _settle(store: CardStore, card: Card, item: WorkItem) -> Card:
    """Move every in-flight stage record to its terminal state from the timeline.

    The work-item slice IS the truth about whether a stage finished; this only
    projects it onto the card's dispatch record so the sequence knows what is left.
    """
    updates: dict[str, dict[str, Any]] = {}
    for stage in SEQUENCE:
        if stage_status(card, stage) != DISPATCHED:
            continue
        status = getattr(item, STAGE_SERVICE[stage].value).status
        if not status:
            continue
        if is_failure_or_stuck(status):
            updates[stage.value] = {"status": FAILED, "detail": status}
        elif is_done(status):
            updates[stage.value] = {"status": DONE, "detail": status}
    return _write_runs(store, card, updates) if updates else card


def _advance(
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None,
    transport: httpx.BaseTransport | None,
    on_dispatch: Callable[[str, dict[str, Any]], None] | None,
) -> Card:
    """Dispatch the next queued stage once the previous one has landed.

    There is no new orchestrator: a sequence advances on the SAME completion event
    that already threads the work-item timeline, which is why this is called from
    :func:`apply_status` and nowhere else.
    """
    if any(stage_status(card, s) == DISPATCHED for s in SEQUENCE):
        return card  # a stage is still in flight — nothing to advance yet
    if any(stage_status(card, s) == FAILED for s in SEQUENCE):
        # A failed stage stops the sequence and blocks the card. Remaining stages
        # stay QUEUED so re-running `run` resumes rather than restarting.
        return store.update(card.card_key, {"status": "blocked"}) or card
    nxt = next((s for s in SEQUENCE if stage_status(card, s) == QUEUED), None)
    if nxt is None:
        return card
    result = dispatch_stage(store, card, nxt, settings=settings, transport=transport)
    if on_dispatch is not None:
        on_dispatch(card.card_key, result)
    return store.get(card.card_key) or card


def card_status_for(item: WorkItem, *, remaining: bool = False) -> str | None:
    """Map a work item's live PARR state onto the card status enum.

    The card contract has five columns and gains none here, so PARR's richer
    lifecycle (planned -> building -> verifying -> verdict) collapses onto them
    honestly: anything in flight is ``in_progress``, a failed or stuck stage is
    ``blocked``, and only a *verdict* is ``done``. ``None`` when the item carries
    no stage state at all.

    ``remaining`` says the card still owes later stages (see
    :func:`pending_stages`). A finished stage is then NOT a verdict — a sequenced
    card that flashed ``done`` the moment planning finished and then walked itself
    back when coding started would simply be lying to the board. A finished PLAN
    is never a verdict either, sequence or no sequence: the build comes next.
    """
    for attr in _STAGE_ORDER:
        status = getattr(item, attr).status
        if not status:
            continue
        if is_failure_or_stuck(status):
            return "blocked"
        if is_done(status):
            return "in_progress" if remaining or attr == "pfactory" else "done"
        return "in_progress"
    return None


def apply_status(
    cards: CardStore,
    item: WorkItem,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    on_dispatch: Callable[[str, dict[str, Any]], None] | None = None,
) -> Card | None:
    """Write a work item's live state back onto the card joined to it.

    Called from every completion-event ingress after the work item is updated.
    Settles the in-flight stage's dispatch record, advances an explicitly-driven
    sequence to its next stage, and then maps the status — which is why the status
    write-back keeps working across a sequence instead of only for a single stage.

    A no-op when no card is joined to this correlation key (the ordinary
    GitHub-issue case) and nothing changed.
    """
    card = cards.get_by_correlation_key(item.correlation_key)
    if card is None:
        return None
    settled = _settle(cards, card, item)
    advanced = _advance(
        cards, settled, settings=settings, transport=transport, on_dispatch=on_dispatch
    )
    status = card_status_for(item, remaining=bool(pending_stages(advanced)))
    if status is None or status == advanced.status:
        return advanced if advanced != card else None
    return cards.update(card.card_key, {"status": status})

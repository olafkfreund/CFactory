"""Card <-> git-provider issue sync (RFC-0019 §3.5 Phase 6; RFC-0020 phase 1).

**The governing law: the git provider is the record of truth (RFC-0003). The
board is a planning projection.** Everything in this module follows from that one
sentence, so it is worth stating the consequences explicitly rather than leaving
them to be inferred from the code:

*Mirrored fields* — ``title``, ``issue_state`` (open/closed), ``labels``, and the
``done`` end of ``status``. These are the provider's. On a conflict — both sides
changed since the last sync — **the provider wins** (on a GitHub deploy, that is
Phase 6's "GitHub wins" verbatim): the card is overwritten, the local edit is
lost, and that is the intended semantics, not a bug. A planner who renames a card
and then finds the issue's title back after a sync is seeing the rule work.

*Planning-only fields* — ``priority``, ``tier``, ``milestone``,
``acceptance_criteria``, ``assignee``, ``correlation_key``. The provider has no
opinion on these (they are the board's reason to exist), so a sync never touches
them. There is no field that both sides own, which is what keeps "the provider
wins" a one-line rule instead of a merge algorithm.

Direction of travel:

* **card -> issue**: one write, ``create_issue``, and only when the card has no
  issue yet. Nothing else is ever pushed — a later local title edit is NOT
  propagated, because propagating it would make the board a second writer of an
  authoritative field and re-open the conflict we just closed.
* **issue -> card**: ``fetch_issue`` and mirror it down. This is the half that
  keeps the board from lying about work that moved on the host.

**Which host (RFC-0020 phase 1).** Both verbs above are the fleet's canonical
``GitProvider`` protocol, vendored at ``apps/backend/runners/github/`` and
drift-gated — not GitHub URLs. Nothing below this line knows what a repo path or
an issue state looks like on any particular host: ``CFACTORY_GIT_PROVIDER``
selects github (default) / gitlab / azure_devops, and the provider hands back
normalised ``IssueData`` where the number is the host's own identifier (a GitLab
IID, say) and the state is ``open``/``closed`` whatever the host calls it. The
card's columns keep their ``github_*`` names — renaming a column is a migration
with no behavioural payoff, and the RFC-0020 phases that own the settings UI can
carry that if it is ever wanted.

Idempotency has no new column, exactly as with the §3.2 intake: ``issue_ref``
non-NULL *is* "this card already has an issue", so a second sync adopts and
mirrors instead of opening a duplicate. Adopting an issue somebody else filed is
the same code path — set ``issue_ref`` on the card and sync.

Fail-safe, same contract as ``card_intake.dispatch_card``: nothing here raises.
A provider outage records the reason on the card (``github_sync_error``) and
returns ``ok=False`` for the caller to audit. The board keeps serving, the card
does not silently look synced, and no card field is half-written — the mirror is
computed in full and applied as one update, or not applied at all.

**Deferred (not half-built):** there is no webhook receiver and no poller, so
issue -> card mirroring happens when a sync is *asked for* (the explicit
endpoint/tool, or a card write that reaches ``ready``), not the instant someone
closes an issue on github.com. Live inbound sync needs a public webhook endpoint
with signature verification — infrastructure this deployment does not have.
Until then a card can be stale, which is why ``issue_state`` is stored and shown
rather than inferred.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from runners.github.providers.protocol import IssueData

from .cards import Card, CardStore
from .config import Settings, get_settings
from .git_providers import IssueProvider, build_provider, provider_token, run_sync

logger = logging.getLogger(__name__)

# A project path ("owner/repo", or "group/sub/project" on a GitLab with
# subgroups) and a ref to an issue in one ("owner/repo#123"). Anchored and
# character-restricted because these strings end up interpolated into a request
# PATH by the provider: an unvalidated ref carrying "../" or a host would let a
# card write choose which resource we call. Every segment must contain at least
# one alphanumeric, which is what keeps ".." out now that a path may be deeper
# than two segments.
_SEGMENT = "[A-Za-z0-9_.-]*[A-Za-z0-9][A-Za-z0-9_.-]*"
_PROJECT = rf"{_SEGMENT}(?:/{_SEGMENT})+"
_PROJECT_RE = re.compile(rf"^({_PROJECT})$")
_ISSUE_REF_RE = re.compile(rf"^({_PROJECT})#([0-9]+)$")


@dataclass(frozen=True)
class IssueRef:
    """A parsed, validated ``<project path>#<number>``.

    Provider-neutral: ``project`` is whatever path the configured host addresses
    a repository by, and ``number`` is whatever integer that host names an issue
    by — a GitHub issue number, a GitLab IID. The board stores and compares it;
    only the provider interprets it.
    """

    project: str
    number: int

    def __str__(self) -> str:
        return f"{self.project}#{self.number}"

    @classmethod
    def parse(cls, text: str | None) -> IssueRef | None:
        """The ref, or ``None`` if it is absent or malformed."""
        match = _ISSUE_REF_RE.match((text or "").strip())
        if match is None:
            return None
        return cls(match.group(1), int(match.group(2)))


def sync_enabled(settings: Settings) -> bool:
    """True when issue sync is configured at all.

    Unconfigured is the default and means OFF: a card write makes no network
    call and opens no issue. This is what keeps the sync inert for every deploy
    that has not opted in — and note what "configured" means: an explicitly set
    ``CFACTORY_GIT_PROVIDER_TOKEN``/``CFACTORY_GITHUB_TOKEN``, never the ambient
    ``GITHUB_TOKEN``/``GH_TOKEN`` a logged-in ``gh`` leaves lying around.
    """
    return bool(provider_token(settings))


def _issue_body(card: Card) -> str:
    """The issue body a card opens with: the plan, plus the card id to trace back."""
    lines = [f"_Planned on the CFactory board as `{card.card_key}`._"]
    if card.acceptance_criteria:
        lines += ["", "## Acceptance Criteria", ""]
        lines += [f"- [ ] {ac}" for ac in card.acceptance_criteria]
    return "\n".join(lines)


def _provider(
    project: str, settings: Settings, transport: httpx.BaseTransport | None
) -> IssueProvider:
    """The configured provider, pointed at *project*."""
    return build_provider(settings, project, transport=transport)


def _open_issue(
    card: Card, *, settings: Settings, transport: httpx.BaseTransport | None
) -> tuple[IssueRef, IssueData]:
    """Open a new issue for a card in the configured project.

    Deliberately sends NO labels. The fleet's issue-driven intake (RFC-0011)
    triggers on a ``factory:<tier>`` label, and the card has already been (or is
    about to be) dispatched by the §3.2 intake hook — labelling the issue would
    build the same card twice. That rule is provider-independent: every host's
    ``create_issue`` takes labels, and we pass none to all of them.
    """
    match = _PROJECT_RE.match((settings.github_repo or "").strip())
    if match is None:
        raise ValueError(
            "cannot open an issue: set CFACTORY_GITHUB_REPO to the provider's "
            "project path (e.g. 'owner/repo'), or set the card's issue_ref to "
            "adopt an existing issue"
        )
    project = match.group(1)
    issue = run_sync(
        _provider(project, settings, transport).create_issue(card.title, _issue_body(card))
    )
    if not isinstance(issue.number, int):
        raise ValueError(f"provider returned no issue number for {card.card_key!r}")
    return IssueRef(project, issue.number), issue


def _fetch_issue(
    ref: IssueRef, *, settings: Settings, transport: httpx.BaseTransport | None
) -> IssueData:
    return run_sync(_provider(ref.project, settings, transport).fetch_issue(ref.number))


def _status_for(issue_state: str, card_status: str) -> str | None:
    """The card status the issue's state implies, or ``None`` to leave it be.

    Only the two states a provider actually asserts are mapped — and they are the
    same two everywhere, because the protocol normalises them (GitLab's "opened"
    arrives here as ``open``):

    * **closed** -> ``done``. The work is finished wherever it was finished.
    * **open** while the card says ``done`` -> ``in_progress``. This is the
      conflict case, and the provider wins: a reopened issue means the work is
      NOT done, so the board must stop claiming it is.

    Every other open-issue status (backlog / ready / in_progress / blocked) is
    the board's own planning column, about which the provider has no opinion, so
    it is left exactly as the planner set it.
    """
    if issue_state == "closed":
        return "done"
    if card_status == "done":
        return "in_progress"
    return None


def _mirror(card: Card, ref: IssueRef, issue: IssueData) -> dict[str, Any]:
    """The card fields the issue overwrites — the provider-wins rule, in code.

    Every mirrored value is taken from the issue unconditionally — that IS the
    conflict rule; a local edit is not consulted, so it cannot win. The final
    comparison against the card only drops no-op writes, it never protects a
    local value.

    Reads only normalised ``IssueData``, so nothing host-shaped reaches a card:
    GitHub's ``[{"name": "bug"}]`` and GitLab's ``["bug"]`` have both already
    become ``["bug"]``, and GitLab's ``opened`` has already become ``open``.

    Note what is NOT here: priority, tier, milestone, acceptance_criteria,
    assignee, correlation_key. Those are planning-only and survive a sync
    untouched no matter what the issue says.
    """
    changes: dict[str, Any] = {"github_sync_error": None, "issue_ref": str(ref)}

    if issue.title:
        changes["title"] = issue.title  # Provider wins: the issue's title is the title.

    # Provider wins: labels are the issue's.
    changes["labels"] = [name for name in issue.labels if isinstance(name, str)]

    if issue.state:
        changes["issue_state"] = issue.state
        status = _status_for(issue.state, card.status)
        if status is not None:
            changes["status"] = status  # Provider wins on open/closed.

    return {field: v for field, v in changes.items() if getattr(card, field) != v}


def sync_card(
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Open-or-adopt the card's issue and mirror it back down. Never raises.

    Idempotent: a card that already carries an ``issue_ref`` is adopted and
    mirrored, never re-created, so calling this twice opens one issue.

    On failure the card is marked (``github_sync_error``) and ``ok=False`` is
    returned — the board neither 500s nor pretends the sync happened.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return {"synced": False, "ok": True, "reason": "github sync not configured"}

    ref = IssueRef.parse(card.issue_ref)
    created = ref is None
    try:
        if ref is None:
            ref, issue = _open_issue(card, settings=settings, transport=transport)
        else:
            issue = _fetch_issue(ref, settings=settings, transport=transport)
    except Exception as exc:  # noqa: BLE001 — the never-raises contract IS the
        # feature, and the blast radius widened with RFC-0020: behind the protocol
        # sits third-party provider code we do not control (a malformed GitLab
        # payload is a KeyError, an unimplemented host surface a
        # NotImplementedError). Naming a list of exception types would mean an
        # unlisted one 500s the board — precisely the outcome this clause exists
        # to prevent. Nothing is swallowed: the reason lands on the card (so the
        # board shows it), in the log, and in the return value the caller audits.
        reason = f"{type(exc).__name__}: {exc}"[:512]
        logger.warning("issue sync failed for %s: %s", card.card_key, reason)
        store.update(card.card_key, {"github_sync_error": reason})
        return {"synced": False, "ok": False, "created": False, "error": reason}

    changes = _mirror(card, ref, issue)
    if changes:
        store.update(card.card_key, changes)
    return {
        "synced": True,
        "ok": True,
        "created": created,
        "issue_ref": str(ref),
        "mirrored": sorted(k for k in changes if k != "github_sync_error"),
    }


def maybe_sync(
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any] | None:
    """Sync hook for the card write path (RFC-0019 §3.5).

    ``None`` means "not a sync event". The trigger is the RFC's own wording —
    "creating a ``ready`` card can open/adopt a GitHub issue" — so a card is
    synced once it reaches ``ready``: that is the point at which it stops being
    a private thought and becomes work, which is what belongs in GitHub. A card
    that already carries an ``issue_ref`` syncs on any write, since it has an
    issue to stay honest about.

    A card with no issue is only synced when a repo to open one IN is configured.
    A deploy that sets a token but no ``CFACTORY_GITHUB_REPO`` has opted into
    mirroring adopted issues, not into filing new ones, so the hook stays quiet
    instead of failing every ready card. Asking explicitly (the endpoint / MCP
    tool) still says plainly that no repo is configured.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return None
    if not card.issue_ref and not (card.status == "ready" and settings.github_repo):
        return None
    return sync_card(store, card, settings=settings, transport=transport)

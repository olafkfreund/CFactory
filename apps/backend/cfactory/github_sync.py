"""Card <-> git-provider issue sync (RFC-0019 §3.5 Phase 6; RFC-0020 phase 1).

**The governing law: the git provider is the record of truth (RFC-0003). The
board is a planning projection.** Everything in this module follows from that one
sentence, so it is worth stating the consequences explicitly rather than leaving
them to be inferred from the code:

*Mirrored fields* — ``title``, ``description`` (the issue body, RFC-0020 §3.6),
``issue_state`` (open/closed), ``labels``, and the ``done`` end of ``status``.
These are the provider's. On a conflict — both sides
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

**Which host (RFC-0020 phases 1 and 2).** Both verbs above are the fleet's
canonical ``GitProvider`` protocol, vendored at ``apps/backend/runners/github/``
and drift-gated — not GitHub URLs. Nothing below this line knows what a repo path
or an issue state looks like on any particular host: the TENANT's git
configuration (§3.3) selects github (default) / gitlab / azure_devops and names
the project, resolved once per sync from the tenant-scoped store this module is
handed, and the provider hands back
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

from .audit import AuditStore
from .cards import SYSTEM_ACTOR, Card, CardStore, DuplicateIssueRefError
from .config import Settings, get_settings
from .error_ref import error_reference
from .git_config import PROJECT_PATTERN, GitTarget
from .git_providers import IssueProvider, build_provider, run_sync

logger = logging.getLogger(__name__)

# A ref to an issue in a project ("owner/repo#123"). Anchored and
# character-restricted because these strings end up interpolated into a request
# PATH by the provider: an unvalidated ref carrying "../" or a host would let a
# card write choose which resource we call. The project half is the same pattern
# a stored git config is validated against — one definition, in
# :mod:`cfactory.git_config`, so a project path the config accepts is exactly a
# project path a ref may name.
_ISSUE_REF_RE = re.compile(rf"^({PROJECT_PATTERN})#([0-9]+)$")


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


def sync_enabled(target: GitTarget) -> bool:
    """True when issue sync is configured for THIS TENANT.

    Unconfigured is the default and means OFF: a card write makes no network
    call and opens no issue. This is what keeps the sync inert for every deploy
    that has not opted in — and note what "configured" means: a credential the
    tenant stored (RFC-0020 §3.4) or an explicitly set
    ``CFACTORY_GIT_PROVIDER_TOKEN``/``CFACTORY_GITHUB_TOKEN``, never the ambient
    ``GITHUB_TOKEN``/``GH_TOKEN`` a logged-in ``gh`` leaves lying around.

    Takes the resolved target rather than ``Settings`` since phase 3: the answer
    is now per tenant, and asking the environment would say "off" for a tenant
    that has a perfectly good credential of its own. Answered WITHOUT decrypting
    anything — ``configured`` is presence, not plaintext.

    **A DEGRADED INSTALL COUNTS AS ON (RFC-0020 §3.4 phase 4).** A connection
    somebody installed a GitHub App on, whose last token mint failed, would
    otherwise report "github sync not configured" and return ``ok=True`` — a card
    write that silently does nothing and blames the user for a setup they did
    perform. Letting it through means the mint raises where ``sync_card`` already
    catches, so the reason lands on the card, in the log and in the caller's
    ``ok=False``. Loud, which is the requirement.
    """
    return target.credential.configured or target.credential.installed


def _issue_body(card: Card) -> str:
    """The issue body a card opens with: the plan, plus the card id to trace back."""
    lines = [f"_Planned on the CFactory board as `{card.card_key}`._"]
    if card.acceptance_criteria:
        lines += ["", "## Acceptance Criteria", ""]
        lines += [f"- [ ] {ac}" for ac in card.acceptance_criteria]
    return "\n".join(lines)


def _provider(
    project: str, target: GitTarget, transport: httpx.BaseTransport | None
) -> IssueProvider:
    """The tenant's configured provider, pointed at *project*."""
    return build_provider(target, project, transport=transport)


def _open_issue(
    card: Card, *, target: GitTarget, transport: httpx.BaseTransport | None
) -> tuple[IssueRef, IssueData]:
    """Open a new issue for a card in the tenant's configured project.

    The only labels sent are the tenant's ``default_labels``, and an
    ``factory:<tier>`` label can never be among them: the fleet's issue-driven
    intake (RFC-0011) triggers on exactly that label, and the card has already
    been (or is about to be) dispatched by the §3.2 intake hook — labelling the
    issue with a tier would build the same card twice. The configuration refuses
    such a label on the way in (``git_config.validate_labels``), so it cannot
    reach here whatever the tenant typed. That rule is provider-independent.
    """
    project = target.project
    if project is None:
        raise ValueError(
            "cannot open an issue: no project is configured for this tenant — set one "
            "in Settings > Git integration (or set the card's issue_ref to adopt an "
            "existing issue)"
        )
    labels = list(target.default_labels) or None
    issue = run_sync(
        _provider(project, target, transport).create_issue(card.title, _issue_body(card), labels)
    )
    if not isinstance(issue.number, int):
        raise ValueError(f"provider returned no issue number for {card.card_key!r}")
    return IssueRef(project, issue.number), issue


def _fetch_issue(
    ref: IssueRef, *, target: GitTarget, transport: httpx.BaseTransport | None
) -> IssueData:
    return run_sync(_provider(ref.project, target, transport).fetch_issue(ref.number))


def _is_missing(exc: Exception) -> bool:
    """True when the host said the issue is not there (RFC-0020 §3.6).

    Only a 404 counts. A 403, a timeout or a malformed payload mean "we could not
    read it", which is a stale card, not a gone issue — recording ``missing`` for
    those would turn a rate limit into a board full of phantom deletions.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404  # noqa: PLR2004


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

    # Provider wins: the issue's body is the card's description (RFC-0020 §3.6
    # classifies it mirrored, exactly like the title, so no field has two owners).
    changes["description"] = issue.body or None

    # Provider wins: labels are the issue's.
    changes["labels"] = [name for name in issue.labels if isinstance(name, str)]

    if issue.state:
        changes["issue_state"] = issue.state
        status = _status_for(issue.state, card.status)
        if status is not None:
            changes["status"] = status  # Provider wins on open/closed.

    return {field: v for field, v in changes.items() if getattr(card, field) != v}


def sync_card(  # noqa: PLR0913 — keyword-only seams: which settings, which transport, and
    # WHO the call is for (#334). An options object would hide the actor at the call
    # sites, which is the one thing this signature exists to make visible.
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    actor: str = SYSTEM_ACTOR,
    audit: AuditStore | None = None,
) -> dict[str, Any]:
    """Open-or-adopt the card's issue and mirror it back down. Never raises.

    Idempotent: a card that already carries an ``issue_ref`` is adopted and
    mirrored, never re-created, so calling this twice opens one issue.

    On failure the card is marked (``github_sync_error``) and ``ok=False`` is
    returned — the board neither 500s nor pretends the sync happened.
    """
    settings = settings or get_settings()
    # THIS CARD's repository (RFC-0020 §3.3 phase 8): the one it names, else the one
    # its issue lives in, else the tenant default. A card imported from a GitLab
    # repo therefore syncs back to GitLab even when the tenant's default repository
    # is on GitHub — the provider, the host and the credential all come from that
    # repository's connection.
    target = store.git_target_for_card(card, settings, actor=actor, audit=audit)
    if not sync_enabled(target):
        return {"synced": False, "ok": True, "reason": "github sync not configured"}

    ref = IssueRef.parse(card.issue_ref)
    created = ref is None
    try:
        if ref is None:
            ref, issue = _open_issue(card, target=target, transport=transport)
        else:
            issue = _fetch_issue(ref, target=target, transport=transport)
    except Exception as exc:  # noqa: BLE001 — the never-raises contract IS the
        # feature, and the blast radius widened with RFC-0020: behind the protocol
        # sits third-party provider code we do not control (a malformed GitLab
        # payload is a KeyError, an unimplemented host surface a
        # NotImplementedError). Naming a list of exception types would mean an
        # unlisted one 500s the board — precisely the outcome this clause exists
        # to prevent. Nothing is swallowed: the FULL failure goes to the log, and
        # a correlation id lands on the card (so the board shows it) and in the
        # return value the caller audits.
        #
        # The exception's own text used to be that reason. It reaches an API
        # response (`POST /cards/{key}:sync`), and it is written by third-party
        # provider code and the stdlib: a DNS failure names an internal host, a
        # file error names a path on disk (CodeQL py/stack-trace-exposure,
        # CWE-209). The id is greppable in the log, where the detail belongs.
        error_id = error_reference(logger, f"issue sync failed for {card.card_key}", exc)
        reason = f"the provider call failed (reference {error_id})"
        changes: dict[str, Any] = {"github_sync_error": reason}
        if _is_missing(exc):
            # Deleted or transferred on the host (RFC-0020 §3.6). The card is
            # marked, NOT deleted: human planning data is not destroyed by a 404.
            changes["issue_state"] = "missing"
        store.update(card.card_key, changes)
        return {"synced": False, "ok": False, "created": False, "error": reason}

    changes = _mirror(card, ref, issue)
    try:
        if changes:
            store.update(card.card_key, changes)
    except DuplicateIssueRefError:
        # Another card in this tenant already holds this issue (RFC-0020 §3.6's
        # unique index). One issue, one card — so this sync stops rather than
        # producing a second projection of the same work.
        reason = f"another card already tracks {ref}"
        store.update(card.card_key, {"github_sync_error": reason})
        return {"synced": False, "ok": False, "created": created, "error": reason}
    return {
        "synced": True,
        "ok": True,
        "created": created,
        "issue_ref": str(ref),
        "mirrored": sorted(k for k in changes if k != "github_sync_error"),
    }


def maybe_sync(  # noqa: PLR0913 — keyword-only seams: which settings, which transport, and
    # WHO the call is for (#334). An options object would hide the actor at the call
    # sites, which is the one thing this signature exists to make visible.
    store: CardStore,
    card: Card,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    actor: str = SYSTEM_ACTOR,
    audit: AuditStore | None = None,
) -> dict[str, Any] | None:
    """Sync hook for the card write path (RFC-0019 §3.5).

    ``None`` means "not a sync event". The trigger is the RFC's own wording —
    "creating a ``ready`` card can open/adopt a GitHub issue" — so a card is
    synced once it reaches ``ready``: that is the point at which it stops being
    a private thought and becomes work, which is what belongs in GitHub. A card
    that already carries an ``issue_ref`` syncs on any write, since it has an
    issue to stay honest about.

    A card with no issue is only synced when the TENANT has a project to open one
    in (RFC-0020 §3.3). A deploy that sets a token but names no project has opted
    into mirroring adopted issues, not into filing new ones, so the hook stays
    quiet instead of failing every ready card. Asking explicitly (the endpoint /
    MCP tool) still says plainly that no project is configured.
    """
    settings = settings or get_settings()
    target = store.git_target_for_card(card, settings, actor=actor, audit=audit)
    if not sync_enabled(target):
        return None
    if not card.issue_ref and not (card.status == "ready" and target.project):
        return None
    return sync_card(store, card, settings=settings, transport=transport, actor=actor, audit=audit)

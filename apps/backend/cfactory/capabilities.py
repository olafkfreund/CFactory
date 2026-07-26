"""What the fleet can actually do on each git host (RFC-0020 §3.5).

**The user story.** I pick GitLab in Settings because that is where my code
lives. Two days later a build finishes and nothing merges, and I spend an
afternoon working out whether I misconfigured something. I did not: GitLab
simply has no ``enable_auto_merge`` in the provider layer. That is a fine answer
— it is just an answer I should have had while I was choosing the provider, not
after a run.

So this module is the answer, published where the choice is made. It is read by
the Settings panel next to the provider selector, by the ``git_capabilities``
MCP tool for an agent configuring the same thing, and by ``docs/guides/
provider-capability-matrix.md`` for anyone deciding before they deploy.

**Why it is data here and not prose in a doc.** A doc drifts silently. This
table is asserted against the vendored provider layer by
``tests/test_capabilities.py``: if a canonical provider grows a real
``enable_auto_merge``, or loses one, the test fails and the matrix has to be
told. That makes the published limitation a checked claim rather than a
remembered one.

**Scope, stated plainly.** These are the capabilities where the fleet's
behaviour DIFFERS by host. Everything else — board sync, issue import, RFC-0011
label intake, and the PARR run itself — works identically on all three, which is
the whole point of vendoring the canonical provider layer in phase 1. A matrix
listing the things that work would be a longer and much less useful document.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .git_config import AZURE_DEVOPS, GITHUB, GITLAB, SUPPORTED_PROVIDERS

# The three support levels, and what each one means for a run. Deliberately
# three and not two: calling GitLab's Duo Workflow delegation "unsupported"
# would be wrong (it dispatches), and calling it "supported" would be worse
# (it silently no-ops without an OAuth-scoped token, so the work never starts
# and nothing says so).
FULL, PARTIAL, NONE = "full", "partial", "none"


@dataclass(frozen=True)
class Capability:
    """One thing the fleet does, and how far each host carries it."""

    key: str
    title: str
    # What the user loses, in the terms they experience it — not in terms of
    # which method raises.
    detail: str
    support: dict[str, str]
    notes: dict[str, str]


# The matrix. Every claim here is traceable to a method on the vendored
# canonical providers under ``runners/github/providers/``.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="board_sync",
        title="Board sync and issue import",
        detail=(
            "Cards open, adopt and mirror issues, and a connected repository's "
            "existing issues import onto the board."
        ),
        support={GITHUB: FULL, GITLAB: FULL, AZURE_DEVOPS: FULL},
        notes={},
    ),
    Capability(
        key="label_intake",
        title="Label intake (RFC-0011)",
        detail=(
            "A factory:low / factory:medium / factory:hard label on an issue "
            "routes it into a build or into planning."
        ),
        support={GITHUB: FULL, GITLAB: FULL, AZURE_DEVOPS: FULL},
        notes={
            AZURE_DEVOPS: (
                "Azure DevOps has tags rather than labels; the provider maps them, "
                "so the trigger reads the same."
            )
        },
    ),
    Capability(
        key="parr",
        title="PARR run (plan, build, verify)",
        detail="Cards dispatch to PFactory, AIFactory and TFactory and report back.",
        support={GITHUB: FULL, GITLAB: FULL, AZURE_DEVOPS: FULL},
        notes={},
    ),
    Capability(
        key="assign_to_user",
        title="Delegate an issue to a coding agent",
        detail=(
            "Assigning an issue to the host's own autonomous agent so it opens "
            "the change itself, instead of AIFactory building it."
        ),
        support={GITHUB: FULL, GITLAB: PARTIAL, AZURE_DEVOPS: NONE},
        notes={
            GITHUB: "GitHub Copilot coding agent.",
            GITLAB: (
                "Dispatches a GitLab Duo Workflow, which needs a Duo entitlement and "
                "an OAuth-scoped token. Without either the call is accepted and the "
                "workflow never starts, so treat a delegated issue as unconfirmed "
                "until the issue itself shows it was picked up."
            ),
            AZURE_DEVOPS: (
                "Raises NotImplementedError. Azure DevOps has no autonomous coding "
                "agent to delegate to, so this is a permanent gap and not a backlog item."
            ),
        },
    ),
    Capability(
        key="enable_auto_merge",
        title="Auto-merge when green",
        detail=(
            "The RFC-0011 low-tier auto-merge-when-green path and the RFC-0009 "
            "merge gate: a reviewed, passing PR merges without a human."
        ),
        support={GITHUB: FULL, GITLAB: NONE, AZURE_DEVOPS: NONE},
        notes={
            GITHUB: "GitHub's native auto-merge.",
            GITLAB: (
                "Raises NotImplementedError. A GitLab run still opens its merge "
                "request and still produces the merge_policy decision — a person or "
                "your CI performs the merge."
            ),
            AZURE_DEVOPS: (
                "Raises NotImplementedError. Same shape as GitLab: the PR is opened "
                "and the decision recorded, completion is manual or CI-driven."
            ),
        },
    ),
    Capability(
        key="auto_pr",
        title="Automatic PR on a clean build",
        detail="AIFactory pushes the build branch and opens the pull request itself.",
        support={GITHUB: FULL, GITLAB: NONE, AZURE_DEVOPS: NONE},
        notes={
            GITLAB: (
                "The endgame is driven through the gh CLI, so it is skipped rather "
                "than attempted — the branch is pushed and the merge request is yours "
                "to open. It is skipped LOUDLY: the run records why."
            ),
            AZURE_DEVOPS: "Skipped, for the same reason as GitLab.",
        },
    ),
)


class CapabilityView(BaseModel):
    """One capability as the panel and the MCP tool see it."""

    key: str
    title: str
    detail: str
    # provider -> "full" | "partial" | "none"
    support: dict[str, str]
    # provider -> the sentence that explains a non-full level (or qualifies a full one)
    notes: dict[str, str]


class CapabilityMatrix(BaseModel):
    """Every capability that differs by host, for every host on offer."""

    providers: list[str]
    capabilities: list[CapabilityView]


def capability_matrix() -> CapabilityMatrix:
    """The published matrix (RFC-0020 §3.5).

    Static: it describes the vendored provider layer, which is the same for
    every tenant of a deployment, so there is nothing tenant-scoped to resolve
    and nothing to cache.
    """
    return CapabilityMatrix(
        providers=list(SUPPORTED_PROVIDERS),
        capabilities=[
            CapabilityView(
                key=cap.key,
                title=cap.title,
                detail=cap.detail,
                support=dict(cap.support),
                notes=dict(cap.notes),
            )
            for cap in CAPABILITIES
        ],
    )


def supports(provider: str, key: str) -> str:
    """How far ``provider`` carries the capability ``key``.

    Unknown provider or unknown capability reads as :data:`NONE` rather than
    raising: a caller asking "may I auto-merge here?" about something this table
    has never heard of must get "no", never an exception it might swallow into a
    "yes".
    """
    kind = (provider or "").strip().lower()
    for cap in CAPABILITIES:
        if cap.key == key:
            return cap.support.get(kind, NONE)
    return NONE

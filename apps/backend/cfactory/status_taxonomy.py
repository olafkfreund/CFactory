"""Canonical status / lifecycle taxonomy for the cross-service correlation store.

The three factories speak different status vocabularies (PFactory board states,
AIFactory ``backlog/in_progress/ai_review/human_review/done``, TFactory
``triaged/passed/…``). The cockpit must map any of those strings to one
lifecycle classification — terminal vs failed vs running vs review vs queued —
and that classification was previously reimplemented several divergent times on
the backend (``store._TERMINAL_HINTS``, ``copilot.anomalies._FAILURE_HINTS`` /
``_TERMINAL_OK``, ``live_agents._INACTIVE_HINTS``). This module is the single
source of truth those call sites consume (#114).

Matching is on EXACT, normalized tokens — never loose substring containment. A
status is normalized to lowercase and split on ``_``, ``-`` and whitespace into
tokens; a classification matches when any token is in the relevant token set (or
the fully normalized status equals a known multi-word marker). This fixes the
prior substring bug where ``"ready"`` matched ``"already"`` and ``"fail"``
matched a status merely containing those letters.

The frontend keeps its own mirror of this taxonomy in
``apps/frontend-web/src/taskState.ts`` (already token-boundary based); these two
are the two canonical definitions, one per runtime.

Convergence note: the previous divergent sets disagreed on a few markers
(``skip``/``skipped`` was inactive-but-not-terminal in ``live_agents`` only, and
``stuck`` was a failure marker in ``anomalies`` only). The canonical sets fold
``skip`` into the healthy-terminal vocabulary (so a skipped stage is preserved as
finished history everywhere, which is the safer default for reconciliation) and
expose ``stuck`` via the dedicated ``is_stuck`` / ``is_failure_or_stuck`` helpers
so the anomaly detector keeps its exact reach without polluting the store's
terminal check.
"""

from __future__ import annotations

import re

# --- Token sets (lifecycle vocabulary) ---------------------------------------
#
# Each token is one normalized status word. A factory status may be a single
# word (``done``) or a compound (``human_review``, ``qa_approved``) that
# normalizes to several tokens; classification matches if ANY token hits the set.

# Failure / rejection markers. A failed stage is terminal AND unhealthy.
_FAILED_TOKENS: frozenset[str] = frozenset(
    {
        "fail",
        "failed",
        "failure",
        "reject",
        "rejected",
        "block",
        "blocked",
        "error",
        "errored",
        "cancel",
        "cancelled",
        "canceled",
        "discard",
        "discarded",
        "abort",
        "aborted",
    }
)

# Successful / completed-OK markers. A done stage is terminal AND healthy.
_DONE_TOKENS: frozenset[str] = frozenset(
    {
        "done",
        "merged",
        "triaged",
        "emitted",
        "complete",
        "completed",
        "accept",
        "accepted",
        "passed",
        "approved",
        "shipped",
        "succeeded",
        "success",
        "ready",
        "closed",
        "skip",
        "skipped",
    }
)

# Parked-for-review markers — finished executing, awaiting a human/AI decision.
# Not terminal: the work item is still in flight.
_REVIEW_TOKENS: frozenset[str] = frozenset(
    {
        "review",
        "reviewing",
        "awaiting",
    }
)

# Queued / not-started markers — no agent attached yet. Not terminal.
_QUEUED_TOKENS: frozenset[str] = frozenset(
    {
        "backlog",
        "pending",
        "queued",
        "todo",
        "icebox",
        "draft",
    }
)

# Actively-executing markers. Used to disambiguate a present-but-unknown status
# (a just-started agent often has no canonical mark yet → treated as running).
_RUNNING_TOKENS: frozenset[str] = frozenset(
    {
        "running",
        "executing",
        "working",
        "coding",
        "planning",
        "generating",
        "building",
        "act",
        "in_progress",
        "inprogress",
        "progress",
        "started",
    }
)

# A stage is "stuck" when an upstream explicitly marks it so (anomaly heuristic).
_STUCK_TOKENS: frozenset[str] = frozenset({"stuck"})

_SPLIT_RE = re.compile(r"[\s_\-]+")


def _tokens(status: str | None) -> set[str]:
    """Normalize a status into its lowercase token set.

    Splits on whitespace, ``_`` and ``-`` so a compound like ``human_review`` or
    ``qa-approved`` yields its parts (``human``, ``review`` / ``qa``,
    ``approved``). Returns an empty set for a falsy status.
    """
    if not status:
        return set()
    return {t for t in _SPLIT_RE.split(status.strip().lower()) if t}


def _matches(status: str | None, vocabulary: frozenset[str]) -> bool:
    """True when any normalized token of *status* is in *vocabulary*."""
    return bool(_tokens(status) & vocabulary)


def is_failed(status: str | None) -> bool:
    """True for a failure / rejection / abort status."""
    return _matches(status, _FAILED_TOKENS)


def is_done(status: str | None) -> bool:
    """True for a successful / completed-OK status (healthy terminal)."""
    return _matches(status, _DONE_TOKENS)


def is_review(status: str | None) -> bool:
    """True for a parked-for-review status (awaiting a human/AI decision)."""
    return _matches(status, _REVIEW_TOKENS)


def is_queued(status: str | None) -> bool:
    """True for a queued / not-started status (no agent attached yet)."""
    return _matches(status, _QUEUED_TOKENS)


def is_stuck(status: str | None) -> bool:
    """True when an upstream explicitly marks a stage stuck."""
    return _matches(status, _STUCK_TOKENS)


def is_terminal(status: str | None) -> bool:
    """True when a stage has reached a terminal state (done OR failed).

    A terminal stage is preserved during reconciliation — completed/failed
    history must persist. Review/queued/running are NOT terminal.
    """
    return is_done(status) or is_failed(status)


def is_failure_or_stuck(status: str | None) -> bool:
    """True for a failure/rejection/abort OR an explicit stuck marker.

    The anomaly detector flags both as worth a human's attention.
    """
    return is_failed(status) or is_stuck(status)


def is_running(status: str | None) -> bool:
    """True when a stage is actively executing (an agent may be live).

    A present-but-unrecognized status reads as running: a just-started agent
    often has no canonical mark yet. A falsy status is NOT running.
    """
    if not status:
        return False
    return not is_terminal(status) and not is_review(status) and not is_queued(status)


def is_active(status: str | None) -> bool:
    """True when a task may have a live agent to stream (#33).

    A missing status reads as active (a just-started agent often has no mark
    yet). False for terminal, review-parked, and queued statuses — none of which
    have a running agent, so their consoles would open and immediately close.
    """
    if not status:
        return True
    return not is_terminal(status) and not is_review(status) and not is_queued(status)

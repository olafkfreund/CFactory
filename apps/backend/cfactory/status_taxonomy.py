"""Canonical status / lifecycle taxonomy for the cross-service correlation store.

The three factories speak different status vocabularies (PFactory board states,
AIFactory ``backlog/in_progress/ai_review/human_review/done``, TFactory
``triaged/passed/…``). The cockpit must map any of those strings to one
lifecycle classification — terminal vs failed vs running vs review vs queued —
and that classification was previously reimplemented several divergent times on
the backend (``store._TERMINAL_HINTS``, ``copilot.anomalies._FAILURE_HINTS`` /
``_TERMINAL_OK``, ``live_agents._INACTIVE_HINTS``). This module is the single
backend surface those call sites consume (#114).

Single source of truth (Factory#160)
------------------------------------
The token sets and classifier logic now live in the Factory hub's GENERATED
``factory-contracts`` package, vendored at
``cfactory._contracts.factory_contracts`` (pinned to a hub SHA; a drift gate
keeps the copy honest — see ``tests/test_factory_contracts_drift.py`` and the
``contracts-drift`` CI job). CFactory is the canonical taxonomy the hub contract
was generated FROM, so importing it here is behaviour-preserving by construction:
the token sets are byte-identical. This module is kept as the backend's import
surface (call sites import ``cfactory.status_taxonomy``) and now RE-EXPORTS the
shared classifiers, so the taxonomy is defined once fleet-wide instead of
hand-rolled per service.

Matching is on EXACT, normalized tokens — never loose substring containment. A
status is normalized to lowercase and split on ``_``, ``-`` and whitespace into
tokens; a classification matches when any token is in the relevant token set.
This fixes the prior substring bug where ``"ready"`` matched ``"already"`` and
``"fail"`` matched a status merely containing those letters.

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

# Re-export the canonical classifiers + token sets from the vendored, pinned
# factory-contracts package (the hub single source of truth). Importing rather
# than redefining is the consumption proven for Factory#160; the names match the
# historic backend surface exactly so every call site is unchanged.
from ._contracts.factory_contracts import (
    _DONE_TOKENS,
    _FAILED_TOKENS,
    _QUEUED_TOKENS,
    _REVIEW_TOKENS,
    _RUNNING_TOKENS,
    _STUCK_TOKENS,
    is_active,
    is_done,
    is_failed,
    is_failure_or_stuck,
    is_queued,
    is_review,
    is_running,
    is_stuck,
    is_terminal,
)

__all__ = [
    "_DONE_TOKENS",
    "_FAILED_TOKENS",
    "_QUEUED_TOKENS",
    "_REVIEW_TOKENS",
    "_RUNNING_TOKENS",
    "_STUCK_TOKENS",
    "is_active",
    "is_done",
    "is_failed",
    "is_failure_or_stuck",
    "is_queued",
    "is_review",
    "is_running",
    "is_stuck",
    "is_terminal",
]

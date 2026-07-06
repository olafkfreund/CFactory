"""Fleet 'needs you' count — how many work items are blocked on a human (#148/#149).

The cockpit already classifies the human-blocked queue on the frontend
(``needsYou.ts``); this is the backend mirror so the count is servable at
``/api/needs-you/count``. Every portal's shared top bar shows the same fleet
number (the forks proxy this endpoint), so "N need you" is consistent across the
family, not just in the cockpit.

An item needs a human when it is parked in a review/approval gate (any stage
whose status classifies as ``review``) or when its active stage has stalled past
its idle deadline (``compute_liveness``). This mirrors ``attentionFor`` in
``needsYou.ts``: the review gate and the stalled hang are exactly its two
non-null branches.
"""

from __future__ import annotations

from .models import WorkItem
from .status_taxonomy import is_review
from .store import compute_liveness

_STAGES = ("pfactory", "aifactory", "tfactory")


def needs_human(wi: WorkItem) -> bool:
    """True when a work item is blocked on a human decision or has stalled."""
    statuses = [getattr(wi, stage).status for stage in _STAGES]
    if any(status and is_review(status) for status in statuses):
        return True
    return compute_liveness(wi).stalled


def needs_you_count(items: list[WorkItem]) -> int:
    """How many of ``items`` are blocked on a human."""
    return sum(1 for wi in items if needs_human(wi))

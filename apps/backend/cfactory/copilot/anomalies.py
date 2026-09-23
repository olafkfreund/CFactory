"""Anomaly detection (#15).

Heuristics over the WorkItem store that flag things worth a human's attention:
failures/gate rejections, repeated handback loops (test→code bouncing), and
stuck stages: an active (non-terminal, non-review) stage whose current status
has not changed for a day. Items parked for review are needs-you, not
anomalies. Cost-spike detection is deferred — the model carries no cost data
yet (see #14). Pure functions; ``now`` is injectable for hermetic tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from cfactory.models import Service, WorkItem
from cfactory.status_taxonomy import is_failure_or_stuck as _is_failure, is_review
from cfactory.store import WorkItemStore, compute_liveness

# An active stage whose status has not changed for this long is "stuck".
_DEFAULT_STALE_SECONDS = 86_400  # 24h


@dataclass
class Anomaly:
    kind: str  # failure | handback_loop | stuck
    severity: str  # high | medium
    correlation_key: str
    title: str | None
    detail: str


def _detect_for_item(wi: WorkItem, now: datetime, stale_seconds: int) -> list[Anomaly]:
    found: list[Anomaly] = []

    # 1. Failure / gate rejection on any stage slice.
    for stage, s in (("plan", wi.pfactory), ("code", wi.aifactory), ("test", wi.tfactory)):
        if _is_failure(s.status):
            found.append(
                Anomaly(
                    "failure",
                    "high",
                    wi.correlation_key,
                    wi.title,
                    f"{stage} stage status={s.status!r}",
                )
            )

    # 2. Handback loop — a failing test event followed by a return to code.
    # Counts test→code bounces. (RFC-0001 idempotency dedups identical events,
    # so we detect bounces by the test-fail→later-code transition, not a raw
    # repeat count.) Single pass (O(n)): a test-failure counts as a handback iff
    # some code event comes after it — i.e. iff its index is before the LAST
    # AIFactory event. Precompute that index once, then count earlier TFactory
    # failures, instead of scanning the tail per event (was O(n^2)).
    tl = wi.timeline
    last_code_idx = -1
    for i, e in enumerate(tl):
        if e.service is Service.AIFACTORY:
            last_code_idx = i
    handbacks = sum(
        1
        for i, e in enumerate(tl)
        if i < last_code_idx and e.service is Service.TFACTORY and _is_failure(e.status)
    )
    if handbacks:
        found.append(
            Anomaly(
                "handback_loop",
                "high",
                wi.correlation_key,
                wi.title,
                f"{handbacks} test-failure→code handback(s) — code↔test bouncing",
            )
        )

    # 3. Stuck — judged on the CURRENT stage status, not the last timeline
    # event: the poll updates slices without appending events, so the timeline
    # can say human_review long after the task finished (#454). A stage parked
    # for review is waiting on a human, not hung; needs_you counts it instead.
    liveness = compute_liveness(wi, now=now, deadline_seconds=stale_seconds)
    if liveness.stalled and not is_review(liveness.active_status):
        hours = int(liveness.last_activity_age_seconds // 3600)
        found.append(
            Anomaly(
                "stuck",
                "medium",
                wi.correlation_key,
                wi.title,
                f"no progress for ~{hours}h "
                f"(last: {liveness.active_service}={liveness.active_status})",
            )
        )
    return found


def detect_anomalies(
    store: WorkItemStore,
    *,
    now: datetime | None = None,
    stale_seconds: int = _DEFAULT_STALE_SECONDS,
) -> list[dict]:
    now = now or datetime.now(UTC)
    out: list[Anomaly] = []
    for wi in store.list():
        out.extend(_detect_for_item(wi, now, stale_seconds))
    return [asdict(a) for a in out]


def anomalies_summary_line(store: WorkItemStore, *, now: datetime | None = None) -> str:
    n = len(detect_anomalies(store, now=now))
    return f"Anomalies: {n} flagged." if n else "Anomalies: none."

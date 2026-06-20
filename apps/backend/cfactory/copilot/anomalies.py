"""Anomaly detection (#15).

Heuristics over the WorkItem store that flag things worth a human's attention:
failures/gate rejections, repeated handback loops (test→code bouncing), and
stuck/stale stages. Cost-spike detection is deferred — the model carries no cost
data yet (see #14). Pure functions; ``now`` is injectable for hermetic tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from cfactory.status_taxonomy import is_done as _is_terminal_ok
from cfactory.status_taxonomy import is_failure_or_stuck as _is_failure

from ..models import Service, WorkItem
from ..store import WorkItemStore

# A stage with no new event for this long (and not terminal) is "stuck".
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

    # 3. Stuck / stale — last event old and not in a terminal-OK state.
    if wi.timeline:
        last = wi.timeline[-1]
        age = (now - last.updated_at).total_seconds()
        if age > stale_seconds and not _is_terminal_ok(last.status):
            hours = int(age // 3600)
            found.append(
                Anomaly(
                    "stuck",
                    "medium",
                    wi.correlation_key,
                    wi.title,
                    f"no progress for ~{hours}h (last: {last.service.value}={last.status})",
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

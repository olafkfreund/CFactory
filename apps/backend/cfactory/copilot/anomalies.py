"""Anomaly detection (#15).

Heuristics over the WorkItem store that flag things worth a human's attention:
failures/gate rejections, repeated handback loops (test→code bouncing), and
stuck/stale stages. Cost-spike detection is deferred — the model carries no cost
data yet (see #14). Pure functions; ``now`` is injectable for hermetic tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from ..models import Service, WorkItem
from ..store import WorkItemStore

_FAILURE_HINTS = ("fail", "reject", "block", "error", "stuck")
_TERMINAL_OK = {"done", "merged", "triaged", "emitted", "completed", "accept",
                "accepted", "passed", "approved"}

# A stage with no new event for this long (and not terminal) is "stuck".
_DEFAULT_STALE_SECONDS = 86_400  # 24h


@dataclass
class Anomaly:
    kind: str            # failure | handback_loop | stuck
    severity: str        # high | medium
    correlation_key: str
    title: str | None
    detail: str


def _is_failure(status: str | None) -> bool:
    return bool(status) and any(h in status.lower() for h in _FAILURE_HINTS)


def _is_terminal_ok(status: str | None) -> bool:
    return bool(status) and status.lower() in _TERMINAL_OK


def _detect_for_item(wi: WorkItem, now: datetime, stale_seconds: int) -> list[Anomaly]:
    found: list[Anomaly] = []

    # 1. Failure / gate rejection on any stage slice.
    for stage, s in (("plan", wi.pfactory), ("code", wi.aifactory), ("test", wi.tfactory)):
        if _is_failure(s.status):
            found.append(Anomaly("failure", "high", wi.correlation_key, wi.title,
                                  f"{stage} stage status={s.status!r}"))

    # 2. Handback loop — a failing test event followed by a return to code.
    # Counts test→code bounces. (RFC-0001 idempotency dedups identical events,
    # so we detect bounces by the test-fail→later-code transition, not a raw
    # repeat count.)
    tl = wi.timeline
    handbacks = sum(
        1
        for i, e in enumerate(tl)
        if e.service is Service.TFACTORY
        and _is_failure(e.status)
        and any(later.service is Service.AIFACTORY for later in tl[i + 1:])
    )
    if handbacks:
        found.append(Anomaly("handback_loop", "high", wi.correlation_key, wi.title,
                             f"{handbacks} test-failure→code handback(s) — code↔test bouncing"))

    # 3. Stuck / stale — last event old and not in a terminal-OK state.
    if wi.timeline:
        last = wi.timeline[-1]
        age = (now - last.updated_at).total_seconds()
        if age > stale_seconds and not _is_terminal_ok(last.status):
            hours = int(age // 3600)
            found.append(Anomaly("stuck", "medium", wi.correlation_key, wi.title,
                                 f"no progress for ~{hours}h (last: {last.service.value}={last.status})"))
    return found


def detect_anomalies(
    store: WorkItemStore,
    *,
    now: datetime | None = None,
    stale_seconds: int = _DEFAULT_STALE_SECONDS,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out: list[Anomaly] = []
    for wi in store.list():
        out.extend(_detect_for_item(wi, now, stale_seconds))
    return [asdict(a) for a in out]


def anomalies_summary_line(store: WorkItemStore, *, now: datetime | None = None) -> str:
    n = len(detect_anomalies(store, now=now))
    return f"Anomalies: {n} flagged." if n else "Anomalies: none."

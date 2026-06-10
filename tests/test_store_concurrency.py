"""Regression: concurrent reads must not hit 'database is locked' on SQLite.

Before WAL + busy_timeout, a GET /api/workitems read colliding with the poll
loop's write raised sqlite3.OperationalError → HTTP 500. This proves the engine
PRAGMAs let a reader run alongside a sustained writer without erroring.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from cfactory.models import CompletionEvent, Service
from cfactory.store import WorkItemStore


def _event(key: str, status: str = "in_progress") -> CompletionEvent:
    return CompletionEvent(
        correlation_key=key,
        service=Service.AIFACTORY,
        task_id=f"task-{key}",
        status=status,
        phase="code",
        updated_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )


def test_concurrent_reads_during_writes_do_not_lock(tmp_path):
    store = WorkItemStore(f"sqlite:///{tmp_path / 'concurrency.db'}")
    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            try:
                store.upsert_from_event(_event(f"k{i % 25}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return
            i += 1

    def reader() -> None:
        while not stop.is_set():
            try:
                store.list()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer) for _ in range(3)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.start()
    # Let them hammer the DB long enough to collide repeatedly.
    threading.Event().wait(2.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"locking errors under concurrency: {errors[:3]}"

"""Regression: concurrent reads must not hit 'database is locked' on SQLite.

Before WAL + busy_timeout, a GET /api/workitems read colliding with the poll
loop's write raised sqlite3.OperationalError → HTTP 500. This proves the engine
PRAGMAs let a reader run alongside a sustained writer without erroring.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime

from cfactory.models import CompletionEvent, Service
from cfactory.store import WorkItemStore


def _event(key: str, status: str = "in_progress") -> CompletionEvent:
    return CompletionEvent(
        correlation_key=key,
        service=Service.AIFACTORY,
        task_id=f"task-{key}",
        status=status,
        phase="code",
        updated_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
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


def test_concurrent_terminal_events_preserve_both_receipts(tmp_path, monkeypatch):
    store = WorkItemStore(f"sqlite:///{tmp_path / 'receipts.db'}")
    key = "registered-p1"
    store.upsert_from_event(_event(key))
    first_read = threading.Event()
    allow_first_write = threading.Event()
    get_row = store._get_row

    def pause_first_read(session, correlation_key, **options):
        row = get_row(session, correlation_key, **options)
        if not first_read.is_set():
            first_read.set()
            assert allow_first_write.wait(5)
        return row

    def complete(task_id):
        return store.upsert_from_event(
            CompletionEvent(
                correlation_key=key,
                service=Service.TFACTORY,
                task_id=task_id,
                id=task_id,
                status="triaged",
            )
        )

    monkeypatch.setattr(store, "_get_row", pause_first_read)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(complete, "handoff")
        try:
            assert first_read.wait(5)
            second = pool.submit(complete, "post-merge")
            with suppress(TimeoutError):
                second.result(timeout=0.5)
        finally:
            allow_first_write.set()
        first.result(timeout=5)
        second.result(timeout=5)

    receipts = [event.id for event in store.get(key).timeline if event.id]
    assert receipts == ["handoff", "post-merge"]

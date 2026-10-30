"""The correlation key is scoped to the repo (#484), and the newest task owns a stage."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from cfactory.adapters import AIFactoryAdapter, hydrate
from cfactory.cards import CardCreate, CardStore
from cfactory.models import CompletionEvent, Service, ServiceState
from cfactory.progress import LiveProgressHub, poll_progress_once


def _adapter(tasks):
    return AIFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"tasks": tasks})),
    )


def test_same_issue_number_in_two_repos_makes_two_work_items(store):
    items = _adapter(
        [
            {"id": "t1", "status": "coding", "githubIssueNumber": 7, "correlationKey": "a/x#7"},
            {"id": "t2", "status": "coding", "githubIssueNumber": 7, "correlationKey": "b/y#7"},
        ]
    ).list_items()
    hydrate(store, items)
    assert sorted(w.correlation_key for w in store.list()) == ["a/x#7", "b/y#7"]


def _poll(store, tasks):
    poll_progress_once(LiveProgressHub(), [_adapter(tasks)], store)
    item = store.get("a/x#7")
    assert item is not None
    return item


def test_newest_stalled_task_owns_the_row_over_an_older_done_one(store):
    item = _poll(
        store,
        [
            {
                "id": "a",
                "status": "done",
                "correlationKey": "a/x#7",
                "createdAt": "2026-06-01T10:00:00Z",
            },
            {
                "id": "c",
                "status": "stalled",
                "correlationKey": "a/x#7",
                "createdAt": "2026-06-02T10:00:00Z",
            },
        ],
    )
    assert item.aifactory.task_id == "c" and item.aifactory.status == "stalled"


def test_missing_created_at_keeps_the_done_task(store):
    item = _poll(
        store,
        [
            {
                "id": "a",
                "status": "done",
                "correlationKey": "a/x#7",
                "createdAt": "2026-06-01T10:00:00Z",
            },
            {"id": "c", "status": "stalled", "correlationKey": "a/x#7"},
        ],
    )
    assert item.aifactory.task_id == "a" and item.aifactory.status == "done"


def test_lone_dead_task_does_not_beat_an_unlisted_rerun(store):
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="a/x#7",
            service=Service.AIFACTORY,
            task_id="rerun",
            status="done",
            updated_at=datetime(2026, 6, 4, tzinfo=UTC),
        )
    )
    item = _poll(
        store,
        [
            {
                "id": "orig",
                "status": "stalled",
                "correlationKey": "a/x#7",
                "createdAt": "2026-06-01T10:00:00Z",
            }
        ],
    )
    assert (item.aifactory.task_id, item.aifactory.status) == ("rerun", "done")


def test_qualified_key_leaves_bare_rows_and_cards_alone(store, tmp_path):
    cards = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    cards.create(CardCreate(title="t", correlation_key="7", issue_ref="a/x#7"))
    store.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="t1", status="coding"))
    item, applied = store.upsert_from_event(
        CompletionEvent(
            correlation_key="a/x#7",
            service=Service.AIFACTORY,
            task_id="t1",
            status="reviewing",
            updated_at=datetime(2026, 6, 4, 12, 5, tzinfo=UTC),
        )
    )
    assert applied and item.correlation_key == "a/x#7"
    assert store.get("7") is not None
    assert cards.get_by_correlation_key("a/x#7") is None

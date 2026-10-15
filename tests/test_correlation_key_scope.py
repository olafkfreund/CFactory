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


def test_bare_row_and_card_are_adopted_by_the_qualified_key(store, tmp_path):
    cards = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    card = cards.create(CardCreate(title="t", correlation_key="7", issue_ref="a/x#7"))
    store.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="t1", status="coding"))
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="7",
            service=Service.AIFACTORY,
            task_id="t1",
            status="coding",
            updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "cost_usd": 0.01},
        )
    )
    before = store.get("7")
    assert before is not None

    event = CompletionEvent(
        correlation_key="a/x#7",
        service=Service.AIFACTORY,
        task_id="t1",
        status="reviewing",
        updated_at=datetime(2026, 6, 4, 12, 5, tzinfo=UTC),
    )
    item, applied = store.upsert_from_event(event)

    assert applied and store.get("7") is None
    assert len(item.timeline) == len(before.timeline) + 1
    assert item.aifactory.usage is not None and item.aifactory.usage.total_tokens == 3
    found = cards.get_by_correlation_key("a/x#7")
    assert found is not None and found.card_key == card.card_key
    assert found.correlation_key == "a/x#7"


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


def test_adoption_keeps_the_stall_clock_and_needs_a_task_id(store):
    store.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="t1", status="coding"))
    before = store.get("7")
    assert before is not None
    # No task id: never adopt (a None match would claim any row lacking the slice).
    store.upsert_snapshot("b/y#7", Service.PFACTORY, ServiceState(status="draft"))
    assert store.get("7") is not None
    # Same task, nothing else changed: renamed, and updated_at (the 24 h pruning clock) kept.
    store.upsert_snapshot("a/x#7", Service.AIFACTORY, ServiceState(task_id="t1", status="coding"))
    after = store.get("a/x#7")
    assert store.get("7") is None and after is not None
    assert after.updated_at == before.updated_at


def test_slice_moves_into_an_existing_qualified_row(store):
    store.upsert_snapshot("a/x#7", Service.PFACTORY, ServiceState(task_id="p1", status="done"))
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="7",
            service=Service.AIFACTORY,
            task_id="t1",
            status="coding",
            updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "cost_usd": 0.01},
        )
    )
    store.upsert_snapshot("a/x#7", Service.AIFACTORY, ServiceState(task_id="t1", status="coding"))
    item = store.get("a/x#7")
    assert store.get("7") is None and item is not None
    assert item.pfactory.task_id == "p1"
    assert item.aifactory.usage is not None and item.aifactory.usage.total_tokens == 3
    assert [e.service for e in item.timeline] == [Service.AIFACTORY]


def test_card_fallback_only_takes_a_card_from_the_keys_repo(tmp_path):
    cards = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    mine = cards.create(CardCreate(title="t", correlation_key="7", issue_ref="a/x#7"))
    cards.create(CardCreate(title="no issue", correlation_key="7"))
    assert cards.get_by_correlation_key("b/y#7") is None
    found = cards.get_by_correlation_key("a/x#7")
    assert found is not None and found.card_key == mine.card_key


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


def _ev(key, status, minute, tokens):
    return CompletionEvent(
        correlation_key=key,
        service=Service.AIFACTORY,
        task_id="t1",
        status=status,
        updated_at=datetime(2026, 6, 4, 12, minute, tzinfo=UTC),
        usage={"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens, "cost_usd": 0.0},
    )


def test_late_old_format_event_does_not_roll_back_the_qualified_slice(store):
    store.upsert_from_event(_ev("a/x#7", "coding", 0, 3))
    store.upsert_from_event(_ev("a/x#7", "reviewing", 5, 10))
    store.upsert_from_event(_ev("7", "coding", 1, 3))  # late event from an old producer pod
    store.upsert_snapshot(
        "a/x#7", Service.AIFACTORY, ServiceState(task_id="t1", status="reviewing")
    )
    item = store.get("a/x#7")
    assert store.get("7") is None and item is not None
    assert item.aifactory.status == "reviewing"
    assert item.aifactory.usage is not None and item.aifactory.usage.total_tokens == 10
    assert [e.updated_at.minute for e in item.timeline if e.updated_at] == [0, 1, 5]

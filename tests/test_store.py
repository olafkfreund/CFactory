"""Unit tests for the WorkItem correlation store (#6)."""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory.models import CompletionEvent, Service, ServiceState
from cfactory.store import _apply_terminal_or_scalar, _attach_access_verification


def _event(service: Service, status: str, key: str = "42") -> CompletionEvent:
    return CompletionEvent(
        correlation_key=key,
        service=service,
        task_id=f"{service.value}-task",
        status=status,
        phase=service.value,
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
    )


def test_create_then_get(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned"))
    wi = store.get("42")
    assert wi is not None
    assert wi.pfactory.task_id == "pfactory-task"
    assert wi.pfactory.status == "planned"
    assert len(wi.timeline) == 1


def test_delete_removes_work_item(store):
    store.upsert_from_event(_event(Service.TFACTORY, "planner_failed", key="bench-1"))
    assert store.get("bench-1") is not None
    assert store.delete("bench-1") is True
    assert store.get("bench-1") is None
    # Idempotent: deleting an already-gone item is a no-op that reports False.
    assert store.delete("bench-1") is False


def test_delete_missing_work_item_returns_false(store):
    assert store.delete("never-existed") is False


def test_repeated_snapshot_same_status_preserves_updated_at(store):
    # A poll re-reporting the SAME non-terminal status must NOT bump updated_at,
    # else a hung stage (gen_functional_initial_started for hours) keeps looking
    # freshly "running" and never trips `stalled` — the cockpit then shows a dead
    # task as running forever (#105).
    s = ServiceState(status="generating", phase="gen_functional_initial_started")
    store.upsert_snapshot("hung", Service.TFACTORY, s)
    first = store.get("hung").updated_at
    store.upsert_snapshot("hung", Service.TFACTORY, s)  # identical re-poll → no-op
    assert store.get("hung").updated_at == first
    # A REAL status change still applies (and advances the slice).
    store.upsert_snapshot("hung", Service.TFACTORY, ServiceState(status="triaged"))
    assert store.get("hung").tfactory.status == "triaged"


# --- #257: a poll must not erase what only an event can carry -----------------


def _usage_event(key: str = "451") -> CompletionEvent:
    """A real AIFactory terminal completion: status + token accounting together."""
    return CompletionEvent(
        correlation_key=key,
        service=Service.AIFACTORY,
        task_id="proj:spec-1",
        status="in_progress",
        phase="coding",
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
        usage={
            "input_tokens": 8580964,
            "output_tokens": 53013,
            "total_tokens": 8633977,
            "cost_usd": 1.661583,
            "model": "claude-haiku-4-5-20251001",
        },
    )


def test_poll_does_not_erase_usage(store):
    """THE DEFECT (#257): `upsert_snapshot` replaced the whole slice, and a polled
    snapshot is structurally blind to token accounting — it always carries
    usage=None. So within one 3-second poll of real usage landing, it was gone, and
    Mission Control's cost widgets read empty on a busy cluster."""
    store.upsert_from_event(_usage_event())
    assert store.get("451").aifactory.usage.total_tokens == 8633977

    # The very next poll of the same task: same status, no usage (it cannot know).
    store.upsert_snapshot(
        "451", Service.AIFACTORY, ServiceState(task_id="proj:spec-1", status="in_progress")
    )
    usage = store.get("451").aifactory.usage
    assert usage is not None, "a poll erased usage it could not have known"
    assert usage.total_tokens == 8633977
    assert usage.cost_usd == 1.661583


def test_poll_with_a_real_status_change_still_keeps_usage(store):
    """The carry-forward must survive a poll that DOES write (status changed) —
    that is the path that actually did the erasing."""
    store.upsert_from_event(_usage_event())
    store.upsert_snapshot(
        "451", Service.AIFACTORY, ServiceState(task_id="proj:spec-1", status="done")
    )
    wi = store.get("451")
    assert wi.aifactory.status == "done"  # the poll's real news still applies
    assert wi.aifactory.usage.total_tokens == 8633977


def test_poll_preserves_the_per_worker_rollups(store):
    """`workers` / `by_provider` / `by_model` are event-only too, and feed the
    billing-mode split in the Usage-by-task panel."""
    store.upsert_from_event(
        CompletionEvent(
            correlation_key="452",
            service=Service.AIFACTORY,
            task_id="proj:spec-2",
            status="in_progress",
            phase="worker",
            updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
            worker={
                "worker_id": "main",
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "total_tokens": 1000,
                "cost_usd": 0.5,
                "billing_mode": "api",
            },
        )
    )
    assert store.get("452").aifactory.workers  # sanity: the event landed
    store.upsert_snapshot(
        "452", Service.AIFACTORY, ServiceState(task_id="proj:spec-2", status="in_progress")
    )
    slice_ = store.get("452").aifactory
    assert slice_.workers, "a poll erased the per-worker map"
    assert slice_.by_provider, "a poll erased the provider rollup"


def test_poll_still_writes_status_and_phase(store):
    """Mutation check, other direction: carrying accounting forward must not turn
    the poll into a no-op. A poll's whole job is to report the current status."""
    store.upsert_from_event(_usage_event())
    store.upsert_snapshot(
        "451", Service.AIFACTORY, ServiceState(task_id="proj:spec-1", status="failed", phase="pr")
    )
    slice_ = store.get("451").aifactory
    assert slice_.status == "failed"
    assert slice_.phase == "pr"


def test_poll_usage_wins_when_the_poll_actually_has_some(store):
    """Mutation check: the carry-forward is a fallback for a BLANK field, not a
    freeze. If a snapshot ever does carry usage, it must take precedence."""
    store.upsert_from_event(_usage_event())
    store.upsert_snapshot(
        "451",
        Service.AIFACTORY,
        ServiceState(
            task_id="proj:spec-1",
            status="in_progress",
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "cost_usd": 0.01},
        ),
    )
    assert store.get("451").aifactory.usage.total_tokens == 3


def test_events_across_services_thread_one_workitem(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned"))
    store.upsert_from_event(_event(Service.AIFACTORY, "coding"))
    store.upsert_from_event(_event(Service.TFACTORY, "triaged"))

    wi = store.get("42")
    assert wi is not None
    # All three service slices populated on the same correlation key.
    assert wi.pfactory.status == "planned"
    assert wi.aifactory.status == "coding"
    assert wi.tfactory.status == "triaged"
    # Timeline accumulates every event in order.
    assert len(wi.timeline) == 3
    assert [e.service for e in wi.timeline] == [
        Service.PFACTORY,
        Service.AIFACTORY,
        Service.TFACTORY,
    ]


def test_latest_event_overwrites_service_slice(store):
    store.upsert_from_event(_event(Service.AIFACTORY, "coding"))
    store.upsert_from_event(_event(Service.AIFACTORY, "done"))
    wi = store.get("42")
    assert wi is not None
    assert wi.aifactory.status == "done"  # slice reflects the latest
    assert len(wi.timeline) == 2  # but history is preserved


def test_list_orders_and_separates_keys(store):
    store.upsert_from_event(_event(Service.PFACTORY, "planned", key="1"))
    store.upsert_from_event(_event(Service.PFACTORY, "planned", key="2"))
    items = store.list()
    assert {wi.correlation_key for wi in items} == {"1", "2"}


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


# --------------------------------------------------------------------------
# #116: upsert_from_event was extracted into 4 documented helpers. These pin
# the extracted helpers directly (the orchestrator behaviour is exercised by
# the wider suite — workers / progress / access / verification tests).
# --------------------------------------------------------------------------


def test_apply_terminal_or_scalar_builds_slice_and_prunes_progress():
    prev = {"worker_progress": {"w1": [{"ts": 1}]}, "workers": {"w1": {"worker_id": "w1"}}}
    # Non-terminal: progress series preserved.
    running = _apply_terminal_or_scalar(prev, _event(Service.AIFACTORY, "coding"))
    assert running["status"] == "coding"
    assert running["worker_progress"] == {"w1": [{"ts": 1}]}
    # Terminal: progress series pruned (finished tasks must not bloat the store).
    done = _apply_terminal_or_scalar(prev, _event(Service.AIFACTORY, "done"))
    assert done["status"] == "done"
    assert done["worker_progress"] == {}


def test_attach_access_and_verification_coexist():
    ev = CompletionEvent(
        correlation_key="42",
        service=Service.TFACTORY,
        task_id="t",
        status="passed",
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
        access={"val3": False, "reason": "no creds"},
        verification={"achieved_level": "VAL-2", "claim": "honest"},
    )
    out = _attach_access_verification({}, ev)
    # Both annotations land under extra together (the original wrote them in two
    # passes; the merge must not drop either).
    assert out["extra"]["access"] == {"val3": False, "reason": "no creds"}
    assert out["extra"]["verification"] == {"achieved_level": "VAL-2", "claim": "honest"}


def test_attach_access_verification_noop_when_absent():
    out = _attach_access_verification({"status": "coding"}, _event(Service.AIFACTORY, "coding"))
    assert "extra" not in out


def test_attach_review_block_lands_under_extra():
    """PFactory's plan-review verdict must reach the cockpit (#245).

    Without it CFactory cannot know a plan is gate-blocked, so it renders an
    ENABLED Approve button beside a `human_review` badge -- the user clicks,
    waits, and gets a 409 naming a lens the card never showed.
    """
    review = {
        "gates_passed": False,
        "threshold": 0.75,
        "aggregate_score": 0.94,
        "lenses": [
            {"lens": "security", "score": 0.70, "findings": [{"title": "No auth criteria"}]},
            {"lens": "clarity", "score": 1.0, "findings": []},
        ],
    }
    ev = CompletionEvent(
        correlation_key="42",
        service=Service.PFACTORY,
        task_id="t",
        status="human_review",
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
        review=review,
    )
    out = _attach_access_verification({}, ev)
    assert out["extra"]["review"] == review
    # The aggregate is deliberately carried but is NOT the test -- every lens
    # must clear the threshold, so 0.94 beside a 0.70 lens still means blocked.
    assert out["extra"]["review"]["gates_passed"] is False


def test_review_block_absent_leaves_the_slice_untouched():
    """PFactory does not send it yet; today's envelopes must ingest unchanged."""
    ev = CompletionEvent(
        correlation_key="42",
        service=Service.PFACTORY,
        task_id="t",
        status="human_review",
        updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
    )
    assert ev.review is None
    out = _attach_access_verification({"status": "human_review"}, ev)
    assert "extra" not in out

"""RFC-0001 completion-event ingress: idempotency + the /completion path (#24).

The collector must be idempotent by (service, correlation_key, status) so a
retried or duplicated delivery is a no-op, and must accept events at the
RFC-documented /api/events/completion path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.models import CompletionEvent, Service


def _event(
    service: Service, status: str, key: str = "142", event_id: str | None = None
) -> CompletionEvent:
    return CompletionEvent(
        id=event_id,
        correlation_key=key,
        service=service,
        task_id=f"{service.value}-task",
        status=status,
        phase=service.value,
        updated_at=datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc),
    )


def _payload(
    service: str, status: str, key: str = "142", event_id: str | None = None
) -> dict:
    p = {
        "correlation_key": key,
        "service": service,
        "task_id": f"{service}-task",
        "status": status,
        "phase": service,
        "updated_at": "2026-06-04T15:00:00+00:00",
    }
    if event_id is not None:
        p["id"] = event_id
    return p


# ── store-level idempotency ──────────────────────────────────────────────────


def test_duplicate_event_is_a_noop(store):
    # #471 cutover: dedup is on the CloudEvents id; a re-delivered event (same id)
    # is the no-op case.
    wi1, applied1 = store.upsert_from_event(
        _event(Service.PFACTORY, "emitted", event_id="dup-1")
    )
    wi2, applied2 = store.upsert_from_event(
        _event(Service.PFACTORY, "emitted", event_id="dup-1")
    )

    assert applied1 is True
    assert applied2 is False  # the retry is a no-op
    assert len(wi2.timeline) == 1  # timeline not double-counted
    assert store.get("142").pfactory.status == "emitted"


def test_same_service_new_status_is_applied(store):
    _, a1 = store.upsert_from_event(_event(Service.AIFACTORY, "coding"))
    _, a2 = store.upsert_from_event(_event(Service.AIFACTORY, "merged"))
    assert a1 is True and a2 is True  # distinct statuses both apply
    assert len(store.get("142").timeline) == 2
    assert store.get("142").aifactory.status == "merged"


def test_cross_service_threads_one_workitem(store):
    store.upsert_from_event(_event(Service.PFACTORY, "emitted"))
    store.upsert_from_event(_event(Service.AIFACTORY, "merged"))
    store.upsert_from_event(_event(Service.TFACTORY, "triaged"))

    wi = store.get("142")
    assert wi.pfactory.status == "emitted"
    assert wi.aifactory.status == "merged"
    assert wi.tfactory.status == "triaged"
    assert len(wi.timeline) == 3  # all three threaded by one key


def test_duplicate_across_services_independent(store):
    # Same status string, different services → not duplicates of each other.
    _, a1 = store.upsert_from_event(_event(Service.PFACTORY, "done"))
    _, a2 = store.upsert_from_event(_event(Service.AIFACTORY, "done"))
    assert a1 is True and a2 is True
    assert len(store.get("142").timeline) == 2


# ── dedup on the envelope id (#468 consumer side) ────────────────────────────


def test_same_id_redelivery_is_a_noop(store):
    """Outbox relay re-delivery of the *same* event (same id) is exactly-once."""
    e = _event(Service.TFACTORY, "triaged", event_id="11111111-1111-4111-8111-1111")
    _, a1 = store.upsert_from_event(e)
    _, a2 = store.upsert_from_event(e)
    assert a1 is True and a2 is False
    assert len(store.get("142").timeline) == 1


def test_same_status_new_id_is_applied_after_handback(store):
    """The collision the legacy (service, status) key got wrong: a legitimate
    re-run after handback emits a NEW event (same service+status, new id) — it
    must be recorded, not swallowed."""
    _, a1 = store.upsert_from_event(
        _event(Service.TFACTORY, "triaged", event_id="round-1")
    )
    _, a2 = store.upsert_from_event(
        _event(Service.TFACTORY, "triaged", event_id="round-2")
    )
    assert a1 is True and a2 is True
    assert len(store.get("142").timeline) == 2


def test_event_without_id_is_recorded_not_deduped(store):
    """#471 cutover: the legacy (service, status) fallback is gone. An id-less
    event is no longer deduped on that key — it is recorded (better to keep a real
    event than swallow it onto a removed key)."""
    _, a1 = store.upsert_from_event(_event(Service.PFACTORY, "emitted"))
    _, a2 = store.upsert_from_event(_event(Service.PFACTORY, "emitted"))
    assert a1 is True and a2 is True  # NOT deduped — both recorded
    assert len(store.get("142").timeline) == 2


def test_event_without_id_logs_ingest_anomaly(store, caplog):
    """#471 cutover: an event without a CloudEvents id is an ingest anomaly
    (every producer stamps one) — surfaced as an error, never a silent fallback.

    The anomaly check runs against an existing timeline, so seed the row first.
    """
    store.upsert_from_event(_event(Service.PFACTORY, "emitted"))  # seed the row
    with caplog.at_level("ERROR", logger="cfactory.store"):
        store.upsert_from_event(_event(Service.PFACTORY, "emitted"))  # hits anomaly
    assert any("missing CloudEvents id" in r.message for r in caplog.records)


def test_event_with_only_time_ingests_and_backfills_updated_at(store):
    """#471: a producer sending CloudEvents `time` (no legacy `updated_at`) still
    ingests, and `updated_at` is backfilled so existing readers keep working."""
    e = CompletionEvent(
        id="time-only-1",
        correlation_key="142",
        service=Service.PFACTORY,
        task_id="pfactory-task",
        status="emitted",
        time=datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc),
    )
    _, applied = store.upsert_from_event(e)
    assert applied is True
    assert e.updated_at == e.time  # backfilled from CloudEvents time


def test_id_present_does_not_log_anomaly(store, caplog):
    """An event WITH a CloudEvents id dedups on the id branch and must NOT trip
    the missing-id ingest anomaly."""
    eid = "22222222-2222-4222-8222-222222222222"
    store.upsert_from_event(_event(Service.PFACTORY, "emitted", event_id=eid))
    with caplog.at_level("ERROR", logger="cfactory.store"):
        store.upsert_from_event(_event(Service.PFACTORY, "emitted", event_id=eid))
    assert not any("missing CloudEvents id" in r.message for r in caplog.records)


def test_id_redelivery_via_http_reports_duplicate(client):
    p = _payload("tfactory", "triaged", event_id="abc-123")
    first = client.post("/api/events/completion", json=p)
    dup = client.post("/api/events/completion", json=p)
    assert first.json()["status"] == "accepted"
    assert dup.json()["status"] == "duplicate"
    wi = client.get("/api/workitems/142").json()
    assert len(wi["timeline"]) == 1
    # The id is retained on the stored timeline entry for traceability.
    assert wi["timeline"][0]["id"] == "abc-123"


# ── HTTP ingress (both paths) ────────────────────────────────────────────────


def test_completion_path_accepts_and_threads(client):
    r = client.post("/api/events/completion", json=_payload("pfactory", "emitted"))
    assert r.status_code == 200
    assert r.json() == {"status": "accepted", "correlation_key": "142"}

    wi = client.get("/api/workitems/142").json()
    assert wi["pfactory"]["status"] == "emitted"


def test_duplicate_post_reports_duplicate(client):
    # #471 cutover: a duplicate POST is detected by the CloudEvents id.
    p = _payload("pfactory", "emitted", event_id="post-dup-1")
    first = client.post("/api/events/completion", json=p)
    dup = client.post("/api/events/completion", json=p)
    assert first.json()["status"] == "accepted"
    assert dup.json()["status"] == "duplicate"

    wi = client.get("/api/workitems/142").json()
    assert len(wi["timeline"]) == 1  # not double-counted via HTTP


def test_legacy_events_path_still_works(client):
    r = client.post("/api/events", json=_payload("tfactory", "triaged"))
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


def test_full_chain_threads_via_http(client):
    client.post("/api/events/completion", json=_payload("pfactory", "emitted"))
    client.post("/api/events/completion", json=_payload("aifactory", "merged"))
    client.post("/api/events/completion", json=_payload("tfactory", "triaged"))

    wi = client.get("/api/workitems/142").json()
    assert [e["service"] for e in wi["timeline"]] == [
        "pfactory",
        "aifactory",
        "tfactory",
    ]


def test_activity_feed_flattens_timeline(client):
    # Activity powers the Audit page; it flattens completion events across items.
    client.post("/api/events/completion", json=_payload("pfactory", "emitted"))
    client.post("/api/events/completion", json=_payload("aifactory", "merged"))

    data = client.get("/api/activity").json()
    assert data["count"] == 2
    assert {e["service"] for e in data["activity"]} == {"pfactory", "aifactory"}
    entry = data["activity"][0]
    assert {"service", "correlation_key", "status", "phase", "updated_at"} <= set(entry)

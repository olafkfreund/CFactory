"""RFC-0001 completion-event ingress: idempotency + the /completion path (#24).

The collector must be idempotent by (service, correlation_key, status) so a
retried or duplicated delivery is a no-op, and must accept events at the
RFC-documented /api/events/completion path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.models import CompletionEvent, Service


def _event(service: Service, status: str, key: str = "142") -> CompletionEvent:
    return CompletionEvent(
        correlation_key=key,
        service=service,
        task_id=f"{service.value}-task",
        status=status,
        phase=service.value,
        updated_at=datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc),
    )


def _payload(service: str, status: str, key: str = "142") -> dict:
    return {
        "correlation_key": key,
        "service": service,
        "task_id": f"{service}-task",
        "status": status,
        "phase": service,
        "updated_at": "2026-06-04T15:00:00+00:00",
    }


# ── store-level idempotency ──────────────────────────────────────────────────


def test_duplicate_event_is_a_noop(store):
    wi1, applied1 = store.upsert_from_event(_event(Service.PFACTORY, "emitted"))
    wi2, applied2 = store.upsert_from_event(_event(Service.PFACTORY, "emitted"))

    assert applied1 is True
    assert applied2 is False                  # the retry is a no-op
    assert len(wi2.timeline) == 1             # timeline not double-counted
    assert store.get("142").pfactory.status == "emitted"


def test_same_service_new_status_is_applied(store):
    _, a1 = store.upsert_from_event(_event(Service.AIFACTORY, "coding"))
    _, a2 = store.upsert_from_event(_event(Service.AIFACTORY, "merged"))
    assert a1 is True and a2 is True          # distinct statuses both apply
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
    assert len(wi.timeline) == 3              # all three threaded by one key


def test_duplicate_across_services_independent(store):
    # Same status string, different services → not duplicates of each other.
    _, a1 = store.upsert_from_event(_event(Service.PFACTORY, "done"))
    _, a2 = store.upsert_from_event(_event(Service.AIFACTORY, "done"))
    assert a1 is True and a2 is True
    assert len(store.get("142").timeline) == 2


# ── HTTP ingress (both paths) ────────────────────────────────────────────────


def test_completion_path_accepts_and_threads(client):
    r = client.post("/api/events/completion", json=_payload("pfactory", "emitted"))
    assert r.status_code == 200
    assert r.json() == {"status": "accepted", "correlation_key": "142"}

    wi = client.get("/api/workitems/142").json()
    assert wi["pfactory"]["status"] == "emitted"


def test_duplicate_post_reports_duplicate(client):
    first = client.post("/api/events/completion", json=_payload("pfactory", "emitted"))
    dup = client.post("/api/events/completion", json=_payload("pfactory", "emitted"))
    assert first.json()["status"] == "accepted"
    assert dup.json()["status"] == "duplicate"

    wi = client.get("/api/workitems/142").json()
    assert len(wi["timeline"]) == 1           # not double-counted via HTTP


def test_legacy_events_path_still_works(client):
    r = client.post("/api/events", json=_payload("tfactory", "triaged"))
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


def test_full_chain_threads_via_http(client):
    client.post("/api/events/completion", json=_payload("pfactory", "emitted"))
    client.post("/api/events/completion", json=_payload("aifactory", "merged"))
    client.post("/api/events/completion", json=_payload("tfactory", "triaged"))

    wi = client.get("/api/workitems/142").json()
    assert [e["service"] for e in wi["timeline"]] == ["pfactory", "aifactory", "tfactory"]

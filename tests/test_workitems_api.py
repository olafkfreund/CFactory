"""API tests for the WorkItem read endpoints (#6)."""

from __future__ import annotations


def _post(client, key, service, status):
    return client.post(
        "/api/events",
        json={
            "correlation_key": key,
            "service": service,
            "task_id": f"{service}-t",
            "status": status,
            "phase": service,
            "updated_at": "2026-06-04T12:00:00Z",
        },
    )


def test_events_persist_and_list(client):
    _post(client, "100", "pfactory", "planned")
    _post(client, "100", "aifactory", "coding")
    _post(client, "200", "tfactory", "triaged")

    resp = client.get("/api/workitems")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    keys = {item["correlation_key"] for item in body["items"]}
    assert keys == {"100", "200"}


def test_get_single_workitem(client):
    _post(client, "100", "pfactory", "planned")
    _post(client, "100", "tfactory", "triaged")

    resp = client.get("/api/workitems/100")
    assert resp.status_code == 200
    wi = resp.json()
    assert wi["pfactory"]["status"] == "planned"
    assert wi["tfactory"]["status"] == "triaged"
    assert len(wi["timeline"]) == 2


def test_get_missing_workitem_404(client):
    resp = client.get("/api/workitems/does-not-exist")
    assert resp.status_code == 404

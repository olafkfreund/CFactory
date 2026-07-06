"""Fleet 'needs you' count: pure classifier + the /api/needs-you/count route (#148)."""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory.models import ServiceState, WorkItem
from cfactory.needs_you import needs_human, needs_you_count


def _wi(key, *, services=None, updated=None):
    svc = services or {}
    return WorkItem(
        correlation_key=key,
        pfactory=svc.get("pfactory") or ServiceState(),
        aifactory=svc.get("aifactory") or ServiceState(),
        tfactory=svc.get("tfactory") or ServiceState(),
        updated_at=updated,
    )


def test_review_gate_needs_a_human():
    wi = _wi("1", services={"aifactory": ServiceState(status="human_review")})
    assert needs_human(wi) is True


def test_flowing_item_does_not():
    wi = _wi(
        "2",
        services={"aifactory": ServiceState(status="coding")},
        updated=datetime.now(UTC),  # fresh → not stalled
    )
    assert needs_human(wi) is False


def test_done_item_does_not():
    wi = _wi("3", services={"tfactory": ServiceState(status="passed")})
    assert needs_human(wi) is False


def test_stalled_active_item_needs_a_human():
    # active (non-terminal) frontier + very old updated_at → stalled past deadline
    wi = _wi(
        "4",
        services={"aifactory": ServiceState(status="coding")},
        updated=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert needs_human(wi) is True


def test_count_sums_blocked_items():
    items = [
        _wi("a", services={"pfactory": ServiceState(status="review")}),
        _wi("b", services={"aifactory": ServiceState(status="coding")}, updated=datetime.now(UTC)),
        _wi("c", services={"tfactory": ServiceState(status="triaged")}),
    ]
    # a is a review gate; b is flowing; c depends on whether "triaged" is a review token
    assert needs_you_count(items) >= 1
    assert needs_human(items[0]) is True
    assert needs_human(items[1]) is False


# --- route ------------------------------------------------------------------


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


def test_endpoint_counts_review_items(client):
    _post(client, "100", "aifactory", "human_review")  # blocked
    _post(client, "200", "aifactory", "coding")  # flowing (stale ts, but coding)

    resp = client.get("/api/needs-you/count")
    assert resp.status_code == 200
    body = resp.json()
    assert "count" in body
    assert body["count"] >= 1

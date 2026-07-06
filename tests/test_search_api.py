"""Federated cross-portal search: pure scoring + the /api/search route (#149)."""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory.models import ServiceState, WorkItem
from cfactory.search import search_workitems


def _wi(key, *, title=None, services=None, updated=None):
    svc = services or {}
    return WorkItem(
        correlation_key=key,
        title=title,
        pfactory=svc.get("pfactory") or ServiceState(),
        aifactory=svc.get("aifactory") or ServiceState(),
        tfactory=svc.get("tfactory") or ServiceState(),
        updated_at=updated,
    )


def test_blank_query_returns_nothing():
    items = [_wi("100", title="anything")]
    assert search_workitems(items, "") == []
    assert search_workitems(items, "   ") == []


def test_exact_key_outranks_title_substring():
    items = [
        _wi("checkout", title="unrelated"),  # substring in key
        _wi("200", title="the checkout flow spec"),  # substring in title
    ]
    results = search_workitems(items, "checkout")
    assert [r["correlation_key"] for r in results] == ["checkout", "200"]
    assert results[0]["matched_on"] == ["key"]


def test_matches_title_case_insensitively():
    items = [_wi("300", title="Add OAuth Login Page")]
    results = search_workitems(items, "oauth")
    assert len(results) == 1
    assert results[0]["correlation_key"] == "300"
    assert "title" in results[0]["matched_on"]


def test_matches_repo_and_reports_services_and_status():
    items = [
        _wi(
            "400",
            title="worker",
            services={
                "aifactory": ServiceState(status="coding", repo="olafkfreund/widgets"),
                "tfactory": ServiceState(status="triaged", repo="olafkfreund/widgets"),
            },
        )
    ]
    results = search_workitems(items, "widgets")
    assert len(results) == 1
    r = results[0]
    assert r["repo"] == "olafkfreund/widgets"
    assert r["services"] == ["aifactory", "tfactory"]
    # furthest-stage status wins (test over build)
    assert r["status"] == "triaged"


def test_no_match_excluded():
    items = [_wi("500", title="alpha"), _wi("600", title="beta")]
    assert search_workitems(items, "gamma") == []


def test_limit_is_respected():
    items = [_wi(str(n), title="repeated match") for n in range(30)]
    assert len(search_workitems(items, "match", limit=5)) == 5


def test_ties_break_on_recency():
    older = _wi("700", title="dup", updated=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _wi("701", title="dup", updated=datetime(2026, 6, 1, tzinfo=UTC))
    results = search_workitems([older, newer], "dup")
    assert [r["correlation_key"] for r in results] == ["701", "700"]


# --- route wiring -----------------------------------------------------------


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


def test_search_endpoint_blank_query(client):
    resp = client.get("/api/search")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"query": "", "count": 0, "results": []}


def test_search_endpoint_finds_by_key(client):
    _post(client, "100", "pfactory", "planned")
    _post(client, "200", "aifactory", "coding")

    resp = client.get("/api/search", params={"q": "100"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["correlation_key"] == "100"
    assert body["results"][0]["services"] == ["pfactory"]

"""Tests for the /api/refresh poll-and-hydrate endpoint (#7-#9)."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from cfactory.adapters import AIFactoryAdapter, PFactoryAdapter, TFactoryAdapter
from cfactory.app import adapters_dep, create_app, store_dep


def _t(payload, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


def test_refresh_hydrates_and_reports_failures(store):
    pf = PFactoryAdapter("http://x", transport=_t(
        [{"session_id": "s1", "board_state": "done", "github_issue": 7, "title": "X"}]))
    ai = AIFactoryAdapter("http://x", transport=_t(
        {"tasks": [{"id": "t1", "status": "coding", "metadata": {"githubIssueNumber": 7}}]}))
    tf = TFactoryAdapter("http://x", transport=_t({"oops": 1}, status=500))  # service down

    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[adapters_dep] = lambda: [pf, ai, tf]
    client = TestClient(app)

    body = client.post("/api/refresh").json()["refreshed"]
    assert body["pfactory"] == 1
    assert body["aifactory"] == 1
    assert isinstance(body["tfactory"], dict) and "error" in body["tfactory"]

    # The two reachable services are threaded onto one WorkItem (key 7).
    wi = client.get("/api/workitems/7").json()
    assert wi["pfactory"]["status"] == "done"
    assert wi["aifactory"]["status"] == "coding"

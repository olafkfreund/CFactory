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
        [{"session_id": "s1", "board_state": "done", "correlation_key": "7", "title": "X"}]))
    ai = AIFactoryAdapter("http://x", transport=_t(
        {"tasks": [{"id": "t1", "status": "coding", "correlation_key": "7"}]}))
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


def test_refresh_in_multi_tenant_writes_like_polling(store, monkeypatch):
    """#521: a tenant-b caller must not receive the deployment-wide results."""
    from cfactory import routes_events
    from cfactory.models import Service, ServiceState

    class _Cfg:
        multi_tenant = True

        def extra_tokens_for(self, service):
            return []

    monkeypatch.setattr(routes_events, "get_settings", _Cfg)
    for tenant, key, tid in (("b", "K2", "t2"), ("c", "K3", "t3"), ("c", "K4", "t4")):
        store.scoped(tenant).upsert_snapshot(
            key, Service.AIFACTORY, ServiceState(task_id=tid, status="coding")
        )
    ai = AIFactoryAdapter("http://x", transport=_t({"tasks": [
        {"id": "t1", "status": "coding", "correlation_key": "K1"},
        {"id": "t2", "status": "done", "correlation_key": "K2"},
        {"id": "t3", "status": "coding", "correlation_key": "K3"},
    ]}))
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store.scoped("b")
    app.dependency_overrides[adapters_dep] = lambda: [ai]
    resp = TestClient(app).post("/api/refresh")

    assert resp.status_code == 200 and "aifactory" in resp.json()["refreshed"]
    assert "K1" not in {w.correlation_key for w in store.scoped("b").list()}
    assert store.scoped("default").get("K1") is not None
    assert store.scoped("b").get("K2").aifactory.status == "done"  # updated in place
    assert store.scoped("c").get("K3").aifactory.status == "coding"  # still live upstream
    assert not store.scoped("c").get("K4").aifactory.status  # gone upstream: cleared like polling

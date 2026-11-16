"""#517: reconcile still runs when extra org keys are set, across every org."""

from __future__ import annotations

import httpx
from cfactory import progress, routes_events
from cfactory.adapters import AIFactoryAdapter
from cfactory.app import adapters_dep, create_app, store_dep
from cfactory.models import Service, ServiceState
from cfactory.progress import LiveProgressHub, poll_progress_once
from fastapi.testclient import TestClient

_ORG_B = "org-b-key"


class _Cfg:
    multi_tenant = False

    def extra_tokens_for(self, service):
        return [_ORG_B]


def _adapter(*, b_down=False):
    def handler(request):
        if request.headers.get("authorization") == f"Bearer {_ORG_B}":
            if b_down:
                return httpx.Response(500)
            return httpx.Response(200, json={"tasks": [{"id": "tb", "status": "coding", "correlation_key": "B"}]})
        return httpx.Response(200, json={"tasks": [{"id": "ta", "status": "coding", "correlation_key": "A"}]})

    return AIFactoryAdapter("http://x", token="primary", transport=httpx.MockTransport(handler))


def _seed(store):
    for key, tid in (("A", "ta"), ("B", "tb"), ("GONE", "tgone")):
        store.upsert_snapshot(key, Service.AIFACTORY, ServiceState(task_id=tid, status="coding"))


def _status(store, key):
    return store.get(key).aifactory.status if store.get(key) else None


def test_poll_reconciles_extra_org_and_keeps_live_ones(store, monkeypatch):
    monkeypatch.setattr(progress, "get_settings", lambda: _Cfg())
    _seed(store)
    poll_progress_once(LiveProgressHub(), [_adapter()], store)
    assert _status(store, "B") == "coding"  # org B still live: not blanked
    assert _status(store, "GONE") in (None, "")  # stale row cleared despite extra orgs


def test_poll_skips_reconcile_when_an_extra_org_is_down(store, monkeypatch):
    monkeypatch.setattr(progress, "get_settings", lambda: _Cfg())
    _seed(store)
    poll_progress_once(LiveProgressHub(), [_adapter(b_down=True)], store)
    assert _status(store, "GONE") == "coding"


def test_refresh_reconciles_with_extra_orgs(store, monkeypatch):
    monkeypatch.setattr(routes_events, "get_settings", lambda: _Cfg())
    _seed(store)
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[adapters_dep] = lambda: [_adapter()]
    TestClient(app).post("/api/refresh")
    assert _status(store, "B") == "coding"
    assert _status(store, "GONE") in (None, "")


def test_poll_logs_warning_when_reconcile_skipped(store, monkeypatch, caplog):
    monkeypatch.setattr(progress, "get_settings", lambda: _Cfg())
    _seed(store)
    with caplog.at_level("WARNING", logger="cfactory.adapters"):
        poll_progress_once(LiveProgressHub(), [_adapter(b_down=True)], store)
    msgs = [r.getMessage() for r in caplog.records if "reconcile skipped" in r.getMessage()]
    assert msgs
    assert "aifactory" in msgs[0]
    assert _ORG_B not in msgs[0]

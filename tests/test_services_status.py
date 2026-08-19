"""Tests for /api/services classifying upstream auth/data-fetch failures.

A reachable-but-rejecting upstream (401/403) must surface as `unauthorized`,
not as a green `online` — the old health probe counted any HTTP response
(even a 401) as alive, masking the failure.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from cfactory.adapters import AIFactoryAdapter, PFactoryAdapter, TFactoryAdapter
from cfactory.app import adapters_dep, create_app, observe_transport_dep


def _t(payload, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


def _offline():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    return httpx.MockTransport(boom)


def test_services_classifies_online_unauthorized_and_offline():
    pf = PFactoryAdapter("http://x", transport=_t({"sessions": []}))         # 200
    ai = AIFactoryAdapter("http://x", transport=_t({}, status=401))          # rejects us
    tf = TFactoryAdapter("http://x", transport=_offline())                   # process down

    app = create_app()
    app.dependency_overrides[adapters_dep] = lambda: [pf, ai, tf]
    client = TestClient(app)

    by_name = {s["name"]: s for s in client.get("/api/services").json()["services"]}

    assert by_name["pfactory"]["online"] is True
    assert by_name["pfactory"]["status"] == "online"

    # The key fix: a 401 is NOT online — it's a distinct, visible auth failure.
    assert by_name["aifactory"]["online"] is False
    assert by_name["aifactory"]["status"] == "unauthorized"

    assert by_name["tfactory"]["online"] is False
    assert by_name["tfactory"]["status"] == "offline"


def _observe_transport(status=200, body="ok"):
    """Mock transport that asserts the probe targets OpenObserve's /healthz path."""

    def handler(request):
        assert request.url.path == "/healthz", request.url.path
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def _three_adapters_up():
    return [
        PFactoryAdapter("http://x", transport=_t({"sessions": []})),
        AIFactoryAdapter("http://x", transport=_t({"tasks": []})),
        TFactoryAdapter("http://x", transport=_t({"results": []})),
    ]


def test_services_includes_observe_entry_and_reports_up_on_200():
    app = create_app()
    app.dependency_overrides[adapters_dep] = _three_adapters_up
    app.dependency_overrides[observe_transport_dep] = lambda: _observe_transport(200, "ok")
    client = TestClient(app)

    services = client.get("/api/services").json()["services"]
    by_name = {s["name"]: s for s in services}

    # Observe appears in the Services view…
    assert "observe" in by_name
    obs = by_name["observe"]
    assert obs["role"] == "Observe"
    # …and reports UP when its real health path (/healthz) returns 200.
    assert obs["online"] is True
    assert obs["status"] == "online"

    # Back-compat: the existing three PARR factories are unaffected.
    assert by_name["pfactory"]["online"] is True
    assert by_name["aifactory"]["online"] is True
    assert by_name["tfactory"]["online"] is True


def test_services_observe_down_when_unreachable():
    app = create_app()
    app.dependency_overrides[adapters_dep] = _three_adapters_up
    app.dependency_overrides[observe_transport_dep] = _offline
    client = TestClient(app)

    by_name = {s["name"]: s for s in client.get("/api/services").json()["services"]}
    assert by_name["observe"]["online"] is False
    assert by_name["observe"]["status"] == "offline"
    # The factories still report up — observe being down doesn't affect them.
    assert by_name["pfactory"]["online"] is True


def test_services_observe_error_on_non_200():
    app = create_app()
    app.dependency_overrides[adapters_dep] = _three_adapters_up
    app.dependency_overrides[observe_transport_dep] = lambda: _observe_transport(503, "down")
    client = TestClient(app)

    by_name = {s["name"]: s for s in client.get("/api/services").json()["services"]}
    assert by_name["observe"]["online"] is False
    assert by_name["observe"]["status"] == "error"

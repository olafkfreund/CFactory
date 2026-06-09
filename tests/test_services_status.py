"""Tests for /api/services classifying upstream auth/data-fetch failures.

A reachable-but-rejecting upstream (401/403) must surface as `unauthorized`,
not as a green `online` — the old health probe counted any HTTP response
(even a 401) as alive, masking the failure.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from cfactory.adapters import AIFactoryAdapter, PFactoryAdapter, TFactoryAdapter
from cfactory.app import adapters_dep, create_app


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

"""Tests for the /api/provider-health aggregator tile (RFC-0008 §3.4, #109)."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from cfactory.app import create_app, provider_health_transport_dep


def _transport(by_host: dict[str, httpx.Response]):
    """MockTransport routing on the request host (each service has its own URL)."""

    def handler(request: httpx.Request) -> httpx.Response:
        # default settings give each service a distinct port on localhost
        port = request.url.port
        return by_host.get(port, httpx.Response(200, json={"status": "healthy"}))

    return httpx.MockTransport(handler)


def _health(any_configured: bool) -> dict:
    return {
        "status": "healthy",
        "version": "9.9.9",
        "provider_auth": {
            "any_configured": any_configured,
            "providers": [
                {"name": "anthropic", "configured": any_configured},
                {"name": "gemini", "configured": False},
            ],
        },
    }


def test_provider_health_flags_a_service_with_no_credentials():
    # pfactory:3105 configured, aifactory:3101 NOT configured, tfactory:3103 configured
    by_port = {
        3105: httpx.Response(200, json=_health(True)),
        3101: httpx.Response(200, json=_health(False)),
        3103: httpx.Response(200, json=_health(True)),
    }
    app = create_app()
    app.dependency_overrides[provider_health_transport_dep] = lambda: _transport(by_port)
    body = TestClient(app).get("/api/provider-health").json()

    by_name = {s["name"]: s for s in body["services"]}
    assert by_name["pfactory"]["any_configured"] is True
    assert by_name["aifactory"]["any_configured"] is False
    assert by_name["aifactory"]["reachable"] is True
    assert by_name["aifactory"]["reported"] is True
    # exactly the one credential-less, reporting service raises the alert
    assert body["alert_count"] == 1


def test_older_service_without_block_is_unknown_not_alerted():
    # a service that doesn't emit provider_auth yet → reported False, no alert
    by_port = {
        3105: httpx.Response(200, json={"status": "healthy"}),
        3101: httpx.Response(200, json=_health(True)),
        3103: httpx.Response(200, json=_health(True)),
    }
    app = create_app()
    app.dependency_overrides[provider_health_transport_dep] = lambda: _transport(by_port)
    body = TestClient(app).get("/api/provider-health").json()
    by_name = {s["name"]: s for s in body["services"]}
    assert by_name["pfactory"]["reported"] is False
    assert by_name["pfactory"]["any_configured"] is False
    assert body["alert_count"] == 0  # unknown is not a false alert


def test_unreachable_service_is_not_alerted():
    def boom(request: httpx.Request) -> httpx.Response:
        if request.url.port == 3101:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=_health(True))

    app = create_app()
    app.dependency_overrides[provider_health_transport_dep] = lambda: httpx.MockTransport(boom)
    body = TestClient(app).get("/api/provider-health").json()
    by_name = {s["name"]: s for s in body["services"]}
    assert by_name["aifactory"]["reachable"] is False
    assert body["alert_count"] == 0  # offline is a reachability concern, not auth

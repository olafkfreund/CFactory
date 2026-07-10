"""Tests for the multi-tenant config flag (#23) as reported by ``/health``.

Per-tenant query *isolation* and full SAML/SCIM remain DEFERRED and are
intentionally not exercised here. The flag is read through ``get_settings()`` in
the ``cfactory.routes_health`` namespace, so we monkeypatch it there rather than
relying on real environment variables.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import cfactory.routes_health as health_router
from cfactory.app import create_app, store_dep
from cfactory.config import Settings


def _patch_multi_tenant(monkeypatch, value: bool) -> Settings:
    """Force the multi_tenant flag where /health reads it, without real env."""
    settings = Settings(multi_tenant=value)
    monkeypatch.setattr(health_router, "get_settings", lambda: settings)
    return settings


def test_health_reports_multi_tenant_false_by_default(monkeypatch, store):
    _patch_multi_tenant(monkeypatch, False)
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    body = TestClient(app).get("/health").json()
    assert body["multi_tenant"] is False


def test_health_reports_multi_tenant_true_when_enabled(monkeypatch, store):
    _patch_multi_tenant(monkeypatch, True)
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    body = TestClient(app).get("/health").json()
    assert body["multi_tenant"] is True

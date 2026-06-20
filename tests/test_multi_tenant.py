"""Tests for the multi-tenant config flip (#23).

Covers the resolution seam :func:`cfactory.enterprise.tenant_id_for` under both
modes of the ``CFACTORY_MULTI_TENANT`` flag, and that ``/health`` reports the
flag. Per-tenant query *isolation* and full SAML/SCIM remain DEFERRED and are
intentionally not exercised here.

Settings are overridden via the repo's seam: the flag is read through
``get_settings()`` in the ``cfactory.enterprise`` / ``cfactory.app`` namespaces,
so we monkeypatch those rather than relying on real environment variables.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.testclient import TestClient

import cfactory.enterprise as enterprise
import cfactory.routes_health as health_router
from cfactory.app import create_app, store_dep
from cfactory.config import Settings
from cfactory.enterprise import DEFAULT_TENANT, tenant_id_for


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [
        (k.lower().encode(), v.encode())
        for k, v in (headers or {}).items()
    ]
    return Request({"type": "http", "headers": raw})


def _patch_multi_tenant(monkeypatch, value: bool) -> Settings:
    """Force the multi_tenant flag everywhere it's read, without real env."""
    settings = Settings(multi_tenant=value)
    monkeypatch.setattr(enterprise, "get_settings", lambda: settings)
    # The /health endpoint lives in the health router and reads get_settings from
    # its own namespace; patch it there so the flag is forced where it's read.
    monkeypatch.setattr(health_router, "get_settings", lambda: settings)
    return settings


# --------------------------------------------------------------------------
# tenant_id_for: flag OFF (default / local)
# --------------------------------------------------------------------------

def test_tenant_default_when_disabled_no_header(monkeypatch):
    _patch_multi_tenant(monkeypatch, False)
    assert tenant_id_for(_request()) == DEFAULT_TENANT


def test_tenant_default_when_disabled_ignores_header(monkeypatch):
    _patch_multi_tenant(monkeypatch, False)
    assert tenant_id_for(_request({"X-Tenant-Id": "acme"})) == DEFAULT_TENANT


# --------------------------------------------------------------------------
# tenant_id_for: flag ON (hosted)
# --------------------------------------------------------------------------

def test_tenant_resolved_from_header_when_enabled(monkeypatch):
    _patch_multi_tenant(monkeypatch, True)
    assert tenant_id_for(_request({"X-Tenant-Id": "acme"})) == "acme"


def test_tenant_falls_back_to_default_when_header_absent(monkeypatch):
    _patch_multi_tenant(monkeypatch, True)
    assert tenant_id_for(_request()) == DEFAULT_TENANT


def test_tenant_falls_back_to_default_when_header_blank(monkeypatch):
    _patch_multi_tenant(monkeypatch, True)
    assert tenant_id_for(_request({"X-Tenant-Id": "   "})) == DEFAULT_TENANT


# --------------------------------------------------------------------------
# /health reports the flag
# --------------------------------------------------------------------------

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

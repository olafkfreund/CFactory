"""Tests for GET /api/settings/token — the token shown on /settings/token (#73)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cfactory.app import create_app
from cfactory.auth import KeyStore, keystore_dep


def _client(keys: dict[str, set[str]] | None) -> TestClient:
    app = create_app()
    if keys is not None:
        app.dependency_overrides[keystore_dep] = lambda: KeyStore(keys)
    return TestClient(app)


def test_settings_token_open_mode():
    resp = _client(None).get("/api/settings/token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "" and body["configured"] is False
    assert "connect_url" in body


def test_settings_token_returns_preferred_key():
    resp = _client({"ro": {"read"}, "rw": {"read", "write"}}).get("/api/settings/token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "rw" and body["configured"] is True

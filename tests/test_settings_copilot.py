"""Runtime-editable copilot settings: provider/model toggle + persistence.

The API key must never be persisted to disk or returned to the client.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cfactory.config import (
    Settings,
    load_copilot_overrides,
    set_copilot_settings,
)
from cfactory.app import create_app


def _settings(tmp_path, **kw):
    return Settings(workspace_root=str(tmp_path), **kw)


def test_set_copilot_settings_persists_provider_and_model_only(tmp_path):
    s = _settings(tmp_path, ollama_api_key="super-secret")
    set_copilot_settings("ollama", "gpt-oss:120b", s)
    assert s.copilot_provider == "ollama"
    assert s.copilot_model == "gpt-oss:120b"

    persisted = json.loads((tmp_path / "copilot-settings.json").read_text())
    assert persisted == {"provider": "ollama", "model": "gpt-oss:120b"}
    # The key is NEVER written to disk.
    assert "super-secret" not in (tmp_path / "copilot-settings.json").read_text()


def test_load_copilot_overrides_applies_persisted(tmp_path):
    set_copilot_settings("ollama", "glm-4.6", _settings(tmp_path))
    fresh = _settings(tmp_path)
    assert fresh.copilot_provider == "claude"  # default before load
    load_copilot_overrides(fresh)
    assert fresh.copilot_provider == "ollama"
    assert fresh.copilot_model == "glm-4.6"


def test_set_copilot_settings_rejects_unknown_provider(tmp_path):
    with pytest.raises(ValueError):
        set_copilot_settings("gpt4all", "x", _settings(tmp_path))


def test_set_copilot_settings_rejects_empty_model(tmp_path):
    with pytest.raises(ValueError):
        set_copilot_settings("claude", "  ", _settings(tmp_path))


def _stub_provider_status(monkeypatch):
    """Keep the endpoint tests hermetic — no network probe of ollama.com."""
    import cfactory.routes_copilot as copilot_router

    monkeypatch.setattr(
        copilot_router,
        "provider_status",
        lambda settings: {
            "provider": settings.copilot_provider,
            "model": settings.copilot_model,
            "reachable": None,
        },
    )


def test_settings_api_get_and_put_roundtrip(monkeypatch, tmp_path):
    # Point the global settings at a temp workspace and a known starting state.
    from cfactory import config as cfg

    monkeypatch.setenv("CFACTORY_WORKSPACE_ROOT", str(tmp_path))
    cfg.reset_settings()
    _stub_provider_status(monkeypatch)

    client = TestClient(create_app())

    got = client.get("/api/settings").json()["copilot"]
    assert got["provider"] == "claude"
    assert "claude" in got["providers"] and "ollama" in got["providers"]
    assert "model" in got
    # No raw key ever leaks — only the has_key flag may appear.
    assert "api_key" not in json.dumps(got)

    put = client.put(
        "/api/settings/copilot", json={"provider": "ollama", "model": "gpt-oss:120b"}
    )
    assert put.status_code == 200
    assert put.json()["copilot"]["provider"] == "ollama"
    assert put.json()["copilot"]["model"] == "gpt-oss:120b"
    # Persisted for the next restart.
    assert (tmp_path / "copilot-settings.json").exists()


def test_settings_api_rejects_bad_provider(monkeypatch, tmp_path):
    from cfactory import config as cfg

    monkeypatch.setenv("CFACTORY_WORKSPACE_ROOT", str(tmp_path))
    cfg.reset_settings()
    _stub_provider_status(monkeypatch)
    client = TestClient(create_app())
    resp = client.put("/api/settings/copilot", json={"provider": "nope", "model": "x"})
    assert resp.status_code == 400

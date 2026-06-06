"""Editable upstream endpoints: runtime override + workspace persistence (#56)."""

from __future__ import annotations

import pytest

from cfactory.config import Settings, load_service_overrides, set_service_url


def _settings(tmp_path):
    return Settings(workspace_root=str(tmp_path))


def test_set_service_url_mutates_and_persists(tmp_path):
    s = _settings(tmp_path)
    set_service_url("pfactory", "http://example:9999", s)
    assert s.pfactory_api_url == "http://example:9999"
    persisted = tmp_path / "service-endpoints.json"
    assert persisted.exists()
    assert "9999" in persisted.read_text()


def test_load_service_overrides_applies_persisted(tmp_path):
    set_service_url("aifactory", "http://ai-host:1234", _settings(tmp_path))
    fresh = _settings(tmp_path)
    assert fresh.aifactory_api_url == "http://localhost:3101"  # default before load
    load_service_overrides(fresh)
    assert fresh.aifactory_api_url == "http://ai-host:1234"


def test_set_service_url_rejects_unknown_service(tmp_path):
    with pytest.raises(ValueError):
        set_service_url("nope", "http://x:1", _settings(tmp_path))


def test_set_service_url_rejects_bad_scheme(tmp_path):
    with pytest.raises(ValueError):
        set_service_url("pfactory", "ftp://x:1", _settings(tmp_path))


def test_load_service_overrides_noop_without_file(tmp_path):
    s = _settings(tmp_path)
    before = s.pfactory_api_url
    # No file written yet — value preserved, no error.
    load_service_overrides(s)
    assert s.pfactory_api_url == before

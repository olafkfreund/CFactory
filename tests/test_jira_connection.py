"""Jira connection gating and sealed two-part credentials (Factory #3775)."""

from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import logging

import httpx
import pytest
from cfactory import (
    auth,
    cards as cards_module,
    config,
    git_config,
    git_config_ops,
    github_sync,
    issue_import,
    mcp,
)
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from cfactory.credentials import (
    CredentialError,
    GitCredentialRow,
    JiraCredential,
    decode_jira_credential,
)
from cfactory.git_config import SUPPORTED_PROVIDERS
from cfactory.git_providers import build_provider
from fastapi.testclient import TestClient

_KEY = base64.b64encode(b"j1" * 16).decode()
_TOKEN = "jira-TEST-TOKEN-9f3c1a"  # noqa: S105 — fake test credential
_EMAIL = "me@example.com"
_WRITER = "test-writer-key-not-a-credential"
_URL = "/api/tenants/default/git-connections"
_SITE = "https://acme.atlassian.net"


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="jira-test-hmac")  # noqa: S106 — fake test key


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("CFACTORY_CREDENTIAL_KEY", f"v1:{_KEY}")
    monkeypatch.delenv("CFACTORY_JIRA_ENABLED", raising=False)
    config.reset_settings()
    for module in (cards_module, git_config, git_config_ops, github_sync, issue_import):
        monkeypatch.setattr(module, "get_settings", config.get_settings, raising=False)
    yield
    config.reset_settings()


@pytest.fixture
def jira_enabled(monkeypatch):
    monkeypatch.setenv("CFACTORY_JIRA_ENABLED", "true")
    config.reset_settings()
    yield
    config.reset_settings()


@pytest.fixture
def client(cards, audit, monkeypatch, _settings):
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(
        mcp, "action_transport_dep", lambda: httpx.MockTransport(lambda _: httpx.Response(200))
    )
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    config.reset_settings()
    auth.set_keys({_WRITER: {"read", "write"}})
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = lambda: None
    yield TestClient(app, headers={"Authorization": f"Bearer {_WRITER}"})
    auth.reset_keystore()


def _tool_payload(client, name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {_WRITER}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def _jira_connection(client) -> int:
    response = client.post(_URL, json={"provider": "jira", "base_url": _SITE})
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "jira"
    assert response.json()["base_url"] == _SITE
    return response.json()["id"]


def test_jira_is_hidden_and_rejected_while_flag_is_off(client):
    assert "jira" not in SUPPORTED_PROVIDERS
    response = client.post(_URL, json={"provider": "jira", "base_url": _SITE})
    assert response.status_code == 400
    assert "CFACTORY_JIRA_ENABLED" in response.json()["detail"]

    payload = _tool_payload(
        client, "cfactory_create_git_connection", {"provider": "jira", "base_url": _SITE}
    )
    assert "CFACTORY_JIRA_ENABLED" in payload["error"]


@pytest.mark.usefixtures("jira_enabled")
def test_jira_requires_site_and_accepts_only_project_keys(client):
    missing = client.post(_URL, json={"provider": "jira"})
    assert missing.status_code == 400
    connection_id = _jira_connection(client)
    repository_url = f"{_URL}/{connection_id}/repositories"

    accepted = client.post(repository_url, json={"project": "PROJ"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["project"] == "PROJ"
    for project in ("owner/repo", "proj"):
        rejected = client.post(repository_url, json={"project": project})
        assert rejected.status_code == 400


@pytest.mark.usefixtures("jira_enabled")
def test_jira_credential_is_sealed_and_never_exposed(client, cards, audit, caplog):
    caplog.set_level(logging.DEBUG)
    connection_id = _jira_connection(client)
    response = client.put(
        f"{_URL}/{connection_id}/credential", json={"credential": _TOKEN, "email": _EMAIL}
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert set(response.json()["credential"]) == {
        "configured",
        "source",
        "updated_at",
        "key_version",
    }
    assert response.json()["credential"]["configured"] is True

    row = cards.credential_row(connection_id)
    for column in GitCredentialRow.__table__.columns:
        value = getattr(row, column.name)
        blob = value if isinstance(value, bytes) else str(value).encode()
        for secret in (_TOKEN, _EMAIL):
            assert secret.encode() not in blob
            assert base64.b64encode(secret.encode()) not in blob

    plaintext = cards.connection_credential(connection_id).token()
    pair = decode_jira_credential(plaintext)
    assert (pair.email, pair.api_token) == (_EMAIL, _TOKEN)
    assert _TOKEN not in repr(pair)
    assert _TOKEN not in repr(JiraCredential(_EMAIL, _TOKEN))

    bodies = (response.text, client.get(_URL).text)
    for body in bodies:
        assert _TOKEN not in body
        assert _EMAIL not in body
    for entry in audit.list():
        assert _TOKEN not in entry.model_dump_json()
    for record in caplog.records:
        assert _TOKEN not in record.getMessage()
        assert _TOKEN not in str(record.args or "")


@pytest.mark.usefixtures("jira_enabled")
def test_missing_jira_email_is_a_client_error(client):
    connection_id = _jira_connection(client)
    response = client.put(f"{_URL}/{connection_id}/credential", json={"credential": _TOKEN})
    assert response.status_code == 400


def test_email_is_refused_for_github(client):
    created = client.post(_URL, json={"provider": "github"})
    assert created.status_code == 200
    response = client.put(
        f"{_URL}/{created.json()['id']}/credential",
        json={"credential": _TOKEN, "email": _EMAIL},
    )
    assert response.status_code == 400


def test_opaque_jira_credential_is_rejected_without_echoing_it():
    opaque = "jira-opaque-value-9f3c1a"
    with pytest.raises(CredentialError) as error:
        decode_jira_credential(opaque)
    assert opaque not in str(error.value)


@pytest.mark.usefixtures("jira_enabled")
def test_build_provider_checks_flag_and_vendoring(client, cards, monkeypatch):
    connection_id = _jira_connection(client)
    repository = client.post(f"{_URL}/{connection_id}/repositories", json={"project": "PROJ"})
    assert repository.status_code == 200
    stored = client.put(
        f"{_URL}/{connection_id}/credential", json={"credential": _TOKEN, "email": _EMAIL}
    )
    assert stored.status_code == 200
    target = cards.git_target()

    monkeypatch.delenv("CFACTORY_JIRA_ENABLED")
    config.reset_settings()
    with pytest.raises(ValueError, match="CFACTORY_JIRA_ENABLED"):
        build_provider(target, "PROJ")

    monkeypatch.setenv("CFACTORY_JIRA_ENABLED", "true")
    config.reset_settings()
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    if importlib.util.find_spec("runners.github.providers.jira_provider") is None:
        with pytest.raises(ValueError, match="not vendored"):
            build_provider(target, "PROJ", transport=transport)
    else:
        jira_provider = importlib.import_module("runners.github.providers.jira_provider")
        assert isinstance(
            build_provider(target, "PROJ", transport=transport), jira_provider.JiraProvider
        )

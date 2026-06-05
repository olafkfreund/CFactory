"""Tests for the audit log (#19): GET /api/audit and recording on execute.

Hermetic: the executor uses an injected ``httpx.MockTransport`` and the audit
log uses a temp-SQLite ``AuditStore`` injected via the ``audit_dep`` seam.
"""

from __future__ import annotations

import httpx
import pytest

from cfactory.app import action_transport_dep, audit_dep, create_app, store_dep
from cfactory.audit import AuditStore
from fastapi.testclient import TestClient


class _RecordingTransport(httpx.MockTransport):
    """A MockTransport returning a fixed response (200 ok by default)."""

    def __init__(self, response: httpx.Response | None = None):
        resp = response or httpx.Response(200, json={"ok": True})

        def handler(request: httpx.Request) -> httpx.Response:
            return resp

        super().__init__(handler)


@pytest.fixture
def audit(tmp_path) -> AuditStore:
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}")


@pytest.fixture
def client_with_audit(store, audit):
    transport = _RecordingTransport()
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = lambda: transport
    return TestClient(app), audit


def _execute_body(**overrides):
    body = {
        "kind": "trigger_handoff",
        "correlation_key": "42",
        "target_service": "aifactory",
        "method": "POST",
        "endpoint": "/api/tasks/create-and-run",
        "payload": {"correlation_key": "42", "issue_number": 42},
        "rationale": "hand off",
    }
    body.update(overrides)
    return body


def test_audit_empty_initially(client_with_audit):
    api, _ = client_with_audit
    resp = api.get("/api/audit")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "entries": []}


def test_execute_records_audit_entry(client_with_audit):
    api, audit = client_with_audit

    resp = api.post("/api/actions/execute", json=_execute_body())
    assert resp.status_code == 200
    assert resp.json() == {"status_code": 200, "ok": True, "body": {"ok": True}}

    # Recorded in the store directly.
    entries = audit.list()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor == "cockpit"
    assert entry.kind == "trigger_handoff"
    assert entry.correlation_key == "42"
    assert entry.target_service == "aifactory"
    assert entry.endpoint == "/api/tasks/create-and-run"
    assert entry.status_code == 200
    assert entry.ok is True

    # And visible via GET /api/audit.
    audit_resp = api.get("/api/audit")
    assert audit_resp.status_code == 200
    body = audit_resp.json()
    assert body["count"] == 1
    got = body["entries"][0]
    assert got["actor"] == "cockpit"
    assert got["kind"] == "trigger_handoff"
    assert got["correlation_key"] == "42"
    assert got["target_service"] == "aifactory"
    assert got["endpoint"] == "/api/tasks/create-and-run"
    assert got["status_code"] == 200
    assert got["ok"] is True
    assert "ts" in got


def test_audit_newest_first(client_with_audit):
    api, _ = client_with_audit
    api.post("/api/actions/execute", json=_execute_body(correlation_key="1"))
    api.post("/api/actions/execute", json=_execute_body(correlation_key="2"))

    body = api.get("/api/audit").json()
    assert body["count"] == 2
    assert [e["correlation_key"] for e in body["entries"]] == ["2", "1"]

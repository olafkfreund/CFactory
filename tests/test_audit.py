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
    # Actor comes from the identity seam; OPEN mode (no keys) -> "local".
    assert entry.actor == "local"
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
    assert got["actor"] == "local"
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


# --------------------------------------------------------------------------
# HMAC-anchored audit chain (#21)
# --------------------------------------------------------------------------

import sqlalchemy as sa  # noqa: E402

from cfactory.audit import AuditEntry, compute_entry_hash  # noqa: E402


def _record(store, **overrides):
    body = {
        "actor": "tester",
        "kind": "trigger_handoff",
        "correlation_key": "k",
        "target_service": "aifactory",
        "endpoint": "/api/tasks/create-and-run",
        "status_code": 200,
        "ok": True,
    }
    body.update(overrides)
    return store.record(**body)


def test_genesis_entry_has_no_prev_hash_and_a_hash(audit):
    entry = _record(audit, correlation_key="1")
    assert entry.prev_hash is None
    assert entry.entry_hash  # non-empty hex digest


def test_chain_links_across_records(audit):
    first = _record(audit, correlation_key="1")
    second = _record(audit, correlation_key="2")
    third = _record(audit, correlation_key="3")

    # Each entry's prev_hash is the previous entry's entry_hash.
    assert second.prev_hash == first.entry_hash
    assert third.prev_hash == second.entry_hash
    # Hashes differ.
    assert len({first.entry_hash, second.entry_hash, third.entry_hash}) == 3


def test_verify_true_for_intact_chain(audit):
    for i in range(5):
        _record(audit, correlation_key=str(i))
    assert audit.verify() == []
    assert audit.is_intact() is True


def test_verify_empty_chain_is_intact(audit):
    assert audit.verify() == []
    assert audit.is_intact() is True


def test_verify_detects_mutated_field(audit):
    _record(audit, correlation_key="1")
    target = _record(audit, correlation_key="2")
    _record(audit, correlation_key="3")

    # Tamper directly with the row in the DB, leaving its stored hash stale.
    with audit._session.begin() as session:
        row = session.get(AuditEntry, target.id)
        row.endpoint = "/api/tasks/EVIL"

    breaks = audit.verify()
    assert target.id in breaks
    assert audit.is_intact() is False


def test_verify_detects_swapped_hash(audit):
    e1 = _record(audit, correlation_key="1")
    e2 = _record(audit, correlation_key="2")

    # Overwrite the second entry's stored hash with a forged value.
    with audit._session.begin() as session:
        row = session.get(AuditEntry, e2.id)
        row.entry_hash = "deadbeef" * 8

    breaks = audit.verify()
    assert e2.id in breaks
    assert e1.id not in breaks


def test_verify_detects_deleted_entry(audit):
    _record(audit, correlation_key="1")
    middle = _record(audit, correlation_key="2")
    third = _record(audit, correlation_key="3")

    # Delete the middle entry; the third's prev_hash no longer links.
    with audit._session.begin() as session:
        session.delete(session.get(AuditEntry, middle.id))

    breaks = audit.verify()
    assert third.id in breaks


def test_secret_change_breaks_verification(tmp_path):
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    good = AuditStore(url, hmac_secret="secret-a")
    _record(good, correlation_key="1")
    _record(good, correlation_key="2")
    assert good.is_intact()

    # Re-open with a different secret: the chain no longer verifies.
    wrong = AuditStore(url, create=False, hmac_secret="secret-b")
    assert wrong.is_intact() is False


def test_compute_entry_hash_is_deterministic():
    values = {
        "ts": "2026-06-05T00:00:00+00:00",
        "actor": "local",
        "kind": "trigger_handoff",
        "correlation_key": "42",
        "target_service": "aifactory",
        "endpoint": "/api/tasks/create-and-run",
        "status_code": 200,
        "ok": True,
    }
    h1 = compute_entry_hash("s", values, None)
    h2 = compute_entry_hash("s", values, None)
    assert h1 == h2
    # Changing prev_hash changes the digest.
    assert compute_entry_hash("s", values, "abc") != h1

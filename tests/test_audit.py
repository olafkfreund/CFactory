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
        "kind": "recover",
        "correlation_key": "42",
        "target_service": "aifactory",
        "method": "POST",
        "endpoint": "/api/tasks/ai-7/recover",
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
    _r = resp.json(); assert _r["status_code"] == 200 and _r["ok"] is True and _r["body"] == {"ok": True}

    # Recorded in the store directly.
    entries = audit.list()
    assert len(entries) == 1
    entry = entries[0]
    # Actor comes from the identity seam; OPEN mode (no keys) -> "local".
    assert entry.actor == "local"
    assert entry.kind == "recover"
    assert entry.correlation_key == "42"
    assert entry.target_service == "aifactory"
    assert entry.endpoint == "/api/tasks/ai-7/recover"
    assert entry.status_code == 200
    assert entry.ok is True

    # And visible via GET /api/audit.
    audit_resp = api.get("/api/audit")
    assert audit_resp.status_code == 200
    body = audit_resp.json()
    assert body["count"] == 1
    got = body["entries"][0]
    assert got["actor"] == "local"
    assert got["kind"] == "recover"
    assert got["correlation_key"] == "42"
    assert got["target_service"] == "aifactory"
    assert got["endpoint"] == "/api/tasks/ai-7/recover"
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

from cfactory.audit import AuditEntry, AuditEntryModel, compute_entry_hash  # noqa: E402


def _record(store, **overrides):
    body = {
        "actor": "tester",
        "kind": "recover",
        "correlation_key": "k",
        "target_service": "aifactory",
        "endpoint": "/api/tasks/ai-7/recover",
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


def test_verify_empty_chain_is_intact(audit):
    assert audit.verify() == []


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
    assert good.verify() == []

    # Re-open with a different secret: the chain no longer verifies.
    wrong = AuditStore(url, create=False, hmac_secret="secret-b")
    assert wrong.verify() != []


# --------------------------------------------------------------------------
# Concurrent appends must not fork the chain (#306)
# --------------------------------------------------------------------------

from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from threading import Barrier  # noqa: E402


def _fork_groups(store) -> list[tuple[str, list[int]]]:
    """Parents that more than one entry chains to — i.e. the chain has forked."""
    with store._session() as session:
        parents = session.execute(
            sa.select(AuditEntry.prev_hash, sa.func.count().label("n"))
            .group_by(AuditEntry.prev_hash)
            .having(sa.func.count() > 1)
        ).all()
        return [
            (
                prev,
                list(session.scalars(sa.select(AuditEntry.id).where(AuditEntry.prev_hash.is_(prev)
                     if prev is None else AuditEntry.prev_hash == prev))),
            )
            for prev, _n in parents
        ]


def _append_concurrently(store, *, writers: int = 8, rounds: int = 12) -> None:
    """Drive `writers` threads at `store.record`, released together each round.

    The barrier is what makes this a real reproduction rather than a hopeful
    one: every round has all writers inside `record` at the same time, which is
    exactly the read-then-insert overlap that forked the live chain (#306).
    """
    barrier = Barrier(writers)

    def writer(n: int) -> None:
        for r in range(rounds):
            barrier.wait()
            _record(store, correlation_key=f"{n}-{r}")

    with ThreadPoolExecutor(max_workers=writers) as pool:
        for fut in [pool.submit(writer, i) for i in range(writers)]:
            fut.result()


def test_concurrent_appends_keep_one_unforked_chain(audit):
    """The acceptance test for #306: verify() clean after concurrent writes."""
    _append_concurrently(audit)

    assert _fork_groups(audit) == [], "two entries chained to the same predecessor"
    assert audit.check() == []
    assert audit.verify() == []


def test_concurrent_appends_all_land(audit):
    """Serialising the append must not drop or merge entries."""
    _append_concurrently(audit, writers=6, rounds=10)

    with audit._session() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(AuditEntry)) == 60


# --------------------------------------------------------------------------
# check(): a fork is not a mutation (#306)
# --------------------------------------------------------------------------


def _forge_sibling(store, parent: AuditEntryModel, **overrides):
    """Insert a second entry chaining to `parent`, hashed correctly.

    Reproduces the shape the pre-fix race left on the live cockpit: two rows
    sharing a predecessor, each with a valid HMAC over its own fields.
    """
    from cfactory.audit import _now

    values = {
        "ts": _now().replace(tzinfo=None),
        "actor": "racer",
        "kind": "approve_review",
        "correlation_key": "sibling",
        "target_service": "aifactory",
        "endpoint": "/api/x",
        "status_code": 200,
        "ok": True,
    }
    values.update(overrides)
    with store._session.begin() as session:
        row = AuditEntry(**values, prev_hash=parent.prev_hash)
        row.entry_hash = compute_entry_hash(store._secret, values, parent.prev_hash)
        session.add(row)
        session.flush()
        return row.id


def test_check_calls_a_concurrent_fork_a_fork(audit):
    first = _record(audit, correlation_key="1")
    second = _record(audit, correlation_key="2")
    sibling = _forge_sibling(audit, second)
    _record(audit, correlation_key="3")

    found = audit.check()
    assert [(b.id, b.kind) for b in found] == [(sibling, "forked")]
    assert str(second.id) in found[0].detail
    assert first.id not in [b.id for b in found]


def test_verify_stays_quiet_about_a_fork_but_check_does_not(audit):
    """The standing false positive is gone from the alarm, not from the record."""
    _record(audit, correlation_key="1")
    second = _record(audit, correlation_key="2")
    sibling = _forge_sibling(audit, second)

    assert audit.verify() == []
    assert [b.id for b in audit.check()] == [sibling]


def test_check_flags_a_copied_row_as_a_duplicate(audit):
    """A replayed row shares a parent like a fork does — and is not benign."""
    _record(audit, correlation_key="1")
    target = _record(audit, correlation_key="2")

    with audit._session.begin() as session:
        row = session.get(AuditEntry, target.id)
        copy = AuditEntry(
            ts=row.ts,
            actor=row.actor,
            kind=row.kind,
            correlation_key=row.correlation_key,
            target_service=row.target_service,
            endpoint=row.endpoint,
            status_code=row.status_code,
            ok=row.ok,
            prev_hash=row.prev_hash,
            entry_hash=row.entry_hash,
        )
        session.add(copy)

    kinds = {b.kind for b in audit.check()}
    assert kinds == {"duplicate"}
    assert audit.verify()  # a replay is tamper evidence, not a race


def test_check_flags_a_deleted_entry_as_dangling(audit):
    _record(audit, correlation_key="1")
    middle = _record(audit, correlation_key="2")
    third = _record(audit, correlation_key="3")

    with audit._session.begin() as session:
        session.delete(session.get(AuditEntry, middle.id))

    found = audit.check()
    assert [(b.id, b.kind) for b in found] == [(third.id, "dangling")]
    assert third.id in audit.verify()


def test_check_flags_a_mutated_field_as_mutated(audit):
    _record(audit, correlation_key="1")
    target = _record(audit, correlation_key="2")

    with audit._session.begin() as session:
        session.get(AuditEntry, target.id).endpoint = "/api/tasks/EVIL"

    assert [(b.id, b.kind) for b in audit.check()] == [(target.id, "mutated")]
    assert target.id in audit.verify()


def test_compute_entry_hash_is_deterministic():
    values = {
        "ts": "2026-06-05T00:00:00+00:00",
        "actor": "local",
        "kind": "recover",
        "correlation_key": "42",
        "target_service": "aifactory",
        "endpoint": "/api/tasks/ai-7/recover",
        "status_code": 200,
        "ok": True,
    }
    h1 = compute_entry_hash("s", values, None)
    h2 = compute_entry_hash("s", values, None)
    assert h1 == h2
    # Changing prev_hash changes the digest.
    assert compute_entry_hash("s", values, "abc") != h1

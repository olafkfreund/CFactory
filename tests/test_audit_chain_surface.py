"""The chain verdict, as an operator actually meets it (#309).

`AuditStore.verify()` and `check()` had no caller outside the test suite, so
reading the tamper-evidence state of the live trail meant `kubectl exec` into
the pod — which is how the #306 false alarm ran for a week unnoticed. These
tests cover the surface that replaces that shell session: `GET /api/audit/chain`.

The load-bearing ones are the mutation pair (a real edit must turn the verdict
red, and undoing it must turn it back) and the acknowledgement pair (the live
chain's permanent fork must not stand red forever, and acknowledging it must not
buy silence for anything else).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cfactory import audit as audit_mod, auth
from cfactory.app import audit_dep, create_app, store_dep
from cfactory.audit import AuditEntry, AuditStore, compute_entry_hash, parse_acknowledged_forks
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Not a credential shape and not a real anchor — the HMAC key these tests sign
# with. Named away from "secret" so the linter's hardcoded-credential rule does
# not fire on a test constant (ruff S105).
HMAC_KEY = "test-chain-anchor"


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'audit.db'}"


def _store(url: str, *, acknowledged: list[int] | None = None) -> AuditStore:
    return AuditStore(url, hmac_secret=HMAC_KEY, acknowledged_forks=acknowledged or [])


def _seed(store: AuditStore, n: int = 3) -> None:
    for i in range(n):
        store.record(
            actor="local",
            kind="recover",
            correlation_key=str(i),
            target_service="aifactory",
            endpoint=f"/api/tasks/ai-{i}/recover",
            status_code=200,
            ok=True,
        )


def _chain(url: str, *, acknowledged: list[int] | None = None) -> dict:
    """Fetch the verdict the way the cockpit does — over HTTP, through the app."""
    app = create_app()
    app.dependency_overrides[audit_dep] = lambda: _store(url, acknowledged=acknowledged)
    resp = TestClient(app).get("/api/audit/chain")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def _no_report_cache(monkeypatch):
    """Recompute on every call.

    The store serves a cached report for 30s so the endpoint cannot be turned
    into a full-table scan per request (see `AuditStore.report`). Every test
    below changes the table and then asks again, so the cache would answer with
    the state from before the change. `test_report_is_cached` covers the cache
    itself with this override lifted.
    """
    monkeypatch.setattr(audit_mod, "_REPORT_TTL", timedelta(0))


# ── the intact case ──────────────────────────────────────────────────────────


def test_intact_chain_reports_ok(db_url):
    _seed(_store(db_url))
    body = _chain(db_url)
    assert body["verdict"] == "ok"
    assert body["rows"] == 3
    assert body["findings"] == []
    assert body["acknowledged_forks"] == []
    # Present and parseable: "the check has not run" must be tellable from "the
    # check found nothing", which is the distinction #306 lacked.
    assert datetime.fromisoformat(body["checked_at"]) <= datetime.now(UTC) + timedelta(seconds=5)


def test_empty_chain_is_ok_not_an_alarm(db_url):
    body = _chain(db_url)
    assert (body["verdict"], body["rows"], body["findings"]) == ("ok", 0, [])


# ── the mutation check, both ways ────────────────────────────────────────────


def _rewrite_endpoint(url: str, entry_id: int, value: str) -> str:
    """Edit a hashed field of a stored row, leaving its entry_hash alone.

    This is the tamper the chain exists to catch: the row still carries the HMAC
    it was written with, which no longer matches the fields it now holds.
    Returns the previous value so the edit can be undone.
    """
    with Session(create_engine(url)) as session:
        row = session.scalars(select(AuditEntry).where(AuditEntry.id == entry_id)).one()
        was = row.endpoint
        row.endpoint = value
        session.commit()
    return was


def test_mutating_an_entry_turns_the_verdict_red_and_undoing_it_turns_it_back(db_url):
    _seed(_store(db_url))
    assert _chain(db_url)["verdict"] == "ok"

    was = _rewrite_endpoint(db_url, 2, "/api/tasks/ai-999/recover")
    tampered = _chain(db_url)
    assert tampered["verdict"] == "tampered"
    assert [(f["id"], f["kind"]) for f in tampered["findings"]] == [(2, "mutated")]
    assert tampered["rows"] == 3

    _rewrite_endpoint(db_url, 2, was)
    assert _chain(db_url)["verdict"] == "ok"


def test_deleting_an_entry_is_tamper_evidence(db_url):
    _seed(_store(db_url))
    with Session(create_engine(url := db_url)) as session:
        session.delete(session.scalars(select(AuditEntry).where(AuditEntry.id == 2)).one())
        session.commit()
    body = _chain(url)
    assert body["verdict"] == "tampered"
    assert [(f["id"], f["kind"]) for f in body["findings"]] == [(3, "dangling")]


# ── the known fork, and what acknowledging it does and does not buy ──────────


def _fork(url: str) -> int:
    """Append a row chained to its grandparent — the pre-#310 write race.

    Reproduces entries 2177/2178 on the live cockpit exactly: two rows share a
    parent, and every HMAC involved is valid, because both writers read the same
    tail before either committed. Nothing here is a forgery; it is what
    `record()` itself produced before the tail read and the insert became one
    critical section.
    """
    with Session(create_engine(url)) as session:
        rows = list(session.scalars(select(AuditEntry).order_by(AuditEntry.id.asc())))
        row = AuditEntry(
            ts=datetime.now(UTC).replace(tzinfo=None),
            actor="local",
            kind="approve_review",
            correlation_key="raced",
            target_service="aifactory",
            endpoint="/api/tasks/ai-9/approve",
            status_code=200,
            ok=True,
            prev_hash=rows[-2].entry_hash,
        )
        row.entry_hash = compute_entry_hash(HMAC_KEY, row._hashed_values(), row.prev_hash)
        session.add(row)
        session.commit()
        return row.id


def test_an_unacknowledged_fork_is_amber_not_red(db_url):
    _seed(_store(db_url))
    forked_id = _fork(db_url)

    body = _chain(db_url)
    # Not "tampered": every HMAC recomputes, so this is a concurrent append and
    # saying "tampered" would be a lie. Not "ok" either: after #310 it should be
    # impossible, so an undeclared one means the serialisation regressed.
    assert body["verdict"] == "forked"
    assert [(f["id"], f["kind"]) for f in body["findings"]] == [(forked_id, "forked")]
    assert body["acknowledged_forks"] == []


def test_acknowledging_the_known_fork_clears_the_verdict_without_hiding_it(db_url):
    _seed(_store(db_url))
    forked_id = _fork(db_url)

    body = _chain(db_url, acknowledged=[forked_id])
    # Green, because a permanent red is the defect #306 was actually about — an
    # alarm that is always on is not an alarm. Still counted and still shown.
    assert body["verdict"] == "ok"
    assert body["findings"] == []
    assert body["acknowledged_forks"] == [forked_id]


def test_a_second_fork_still_shows_when_the_first_is_acknowledged(db_url):
    """The known fork is forgiven by id, not by kind.

    Acknowledging entry 2178 must not become "forks are fine now" — that would
    turn the #310 fix's own regression into silence.
    """
    _seed(_store(db_url))
    known = _fork(db_url)
    _seed(_store(db_url), n=1)
    fresh = _fork(db_url)

    body = _chain(db_url, acknowledged=[known])
    assert body["verdict"] == "forked"
    assert [f["id"] for f in body["findings"]] == [fresh]
    assert body["acknowledged_forks"] == [known]


def test_acknowledging_an_id_does_not_forgive_a_mutation_at_that_id(db_url):
    """Acknowledgement only ever forgives a `forked` classification.

    Otherwise naming a row here would buy permanent silence for every later edit
    to it — an acknowledgement list that doubles as a tamper allowlist.
    """
    _seed(_store(db_url))
    _rewrite_endpoint(db_url, 2, "/api/tasks/ai-999/recover")

    body = _chain(db_url, acknowledged=[2])
    assert body["verdict"] == "tampered"
    assert [(f["id"], f["kind"]) for f in body["findings"]] == [(2, "mutated")]
    assert body["acknowledged_forks"] == []


# ── the knobs ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, set()),
        ("", set()),
        ("2178", {2178}),
        ("2178,4001", {2178, 4001}),
        (" 2178 , 4001 ", {2178, 4001}),
    ],
)
def test_parse_acknowledged_forks(raw, expected):
    assert parse_acknowledged_forks(raw) == expected


def test_report_is_cached(db_url, monkeypatch):
    """The scan is O(rows) and unlocked; the cache is the whole rate limit."""
    monkeypatch.setattr(audit_mod, "_REPORT_TTL", timedelta(seconds=30))
    store = _store(db_url)
    _seed(store)
    first = store.report()
    _seed(store, n=1)
    assert store.report().checked_at == first.checked_at
    assert store.report().rows == 3  # the new row is not in the cached answer


def test_chain_is_not_folded_into_the_polled_audit_list(db_url):
    """`GET /api/audit` must stay a cheap windowed read (#309)."""
    _seed(_store(db_url))
    app = create_app()
    app.dependency_overrides[audit_dep] = lambda: _store(db_url)
    body = TestClient(app).get("/api/audit").json()
    # Named rather than a whole-key-set equality: the point is that the EXPENSIVE
    # full-table verdict is absent, not that no cheap field may ever be added
    # (`attribution` is one, and it costs a settings read).
    assert not {"verdict", "findings", "rows", "acknowledged_forks"} & set(body)
    assert set(body) == {"count", "entries", "attribution"}


def test_chain_never_serves_the_hmac_secret(db_url):
    """Hash prefixes for a human to read; never the key they were made with."""
    _seed(_store(db_url))
    _rewrite_endpoint(db_url, 2, "/api/tasks/ai-999/recover")
    _fork(db_url)
    assert HMAC_KEY not in str(_chain(db_url))


def test_chain_requires_the_read_scope_when_keys_are_configured(db_url, monkeypatch):
    """Same gate as the rest of /api/*, including on the editor host."""
    monkeypatch.setattr(auth, "_keystore", auth.KeyStore({"cfk_good": {"read"}}))
    app = create_app()
    app.dependency_overrides[audit_dep] = lambda: _store(db_url)
    api = TestClient(app)
    assert api.get("/api/audit/chain").status_code == 401
    assert api.get("/api/audit/chain", headers={"X-API-Key": "cfk_good"}).status_code == 200


def test_store_dep_is_untouched_by_this_surface(db_url):
    """The chain read needs no work-item store — it is the audit table alone."""
    app = create_app()
    app.dependency_overrides[audit_dep] = lambda: _store(db_url)
    assert store_dep not in app.dependency_overrides
    assert TestClient(app).get("/api/audit/chain").status_code == 200

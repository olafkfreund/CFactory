"""Planning-card CRUD API (RFC-0019 Phase 1, #302).

Covers the five contract points: a CRUD round-trip, tenant isolation (tenant A
can neither read nor modify tenant B's card), write-scope enforcement on every
mutation (a read-only key is insufficient), an audit entry per mutation, and
PATCH driving both the board move (status) and the reprioritise (priority).

The keystore is process-global (the enforcement middleware can't use the
``keystore_dep`` override), so the scope tests configure it via ``set_keys`` and
an autouse fixture restores OPEN mode.
"""

from __future__ import annotations

import pytest
from cfactory import api_deps
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.auth import reset_keystore, set_keys
from cfactory.cards import CardStore
from cfactory.config import Settings
from fastapi.testclient import TestClient

# Not a credential: the HMAC anchor for the temp audit chain in these tests.
_TEST_HMAC = "cards-test-hmac"

RW = {"k_rw": {"read", "write"}}
RO = {"k_ro": {"read"}}


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)


@pytest.fixture
def client(cards, audit):
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_keystore():
    yield
    reset_keystore()


def _tenant_client(monkeypatch, cards, audit) -> TestClient:
    """A client wired to the REAL cards_store_dep with multi-tenancy forced on,
    so X-Tenant-Id actually scopes the store."""
    settings = Settings(multi_tenant=True)
    monkeypatch.setattr(api_deps, "get_settings", lambda: settings)
    monkeypatch.setattr(api_deps, "get_cards_store", lambda: cards)
    app = create_app()
    app.dependency_overrides[audit_dep] = lambda: audit
    return TestClient(app)


# ── CRUD round-trip ──────────────────────────────────────────────────────────


def test_crud_round_trip(client):
    created = client.post(
        "/api/cards",
        json={
            "card_key": "FCT-42",
            "title": "Ship the planning board",
            "acceptance_criteria": ["cards persist", "REST CRUD works"],
            "status": "ready",
            "priority": 2,
            "tier": "medium",
            "assignee": "aifactory",
            "milestone": "v1",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["card_key"] == "FCT-42"
    assert body["acceptance_criteria"] == ["cards persist", "REST CRUD works"]
    assert body["correlation_key"] is None  # planned, not yet in the factory

    assert client.get("/api/cards/FCT-42").json()["title"] == "Ship the planning board"

    listed = client.get("/api/cards").json()
    assert listed["count"] == 1
    assert listed["cards"][0]["card_key"] == "FCT-42"

    patched = client.patch("/api/cards/FCT-42", json={"title": "Ship it", "correlation_key": "302"})
    assert patched.status_code == 200
    assert patched.json()["title"] == "Ship it"
    assert patched.json()["correlation_key"] == "302"  # now joins work_items

    assert client.delete("/api/cards/FCT-42").json()["deleted"] is True
    assert client.get("/api/cards/FCT-42").status_code == 404
    assert client.get("/api/cards").json()["count"] == 0


def test_unknown_card_is_404(client):
    assert client.get("/api/cards/nope").status_code == 404
    assert client.patch("/api/cards/nope", json={"status": "done"}).status_code == 404
    assert client.delete("/api/cards/nope").status_code == 404


def test_duplicate_card_key_is_409(client):
    client.post("/api/cards", json={"card_key": "FCT-1", "title": "one"})
    dup = client.post("/api/cards", json={"card_key": "FCT-1", "title": "again"})
    assert dup.status_code == 409


def test_card_key_is_assigned_when_omitted(client):
    first = client.post("/api/cards", json={"title": "no key given"}).json()
    second = client.post("/api/cards", json={"title": "nor here"}).json()
    assert first["card_key"] == "FCT-1"
    assert second["card_key"] == "FCT-2"


def test_invalid_status_is_rejected(client):
    resp = client.post("/api/cards", json={"card_key": "FCT-9", "title": "t", "status": "wat"})
    assert resp.status_code == 422


# ── a blank acceptance criterion is not a criterion (#324) ───────────────────


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_acceptance_criterion_is_refused_on_create(client, blank):
    """When a `ready` card dispatches these become the RFC-0002 task contract's
    acceptance criteria, and a blank one is satisfied by anything - a green
    verdict against nothing, which reads like a check that ran."""
    resp = client.post("/api/cards", json={"title": "t", "acceptance_criteria": ["real", blank]})

    assert resp.status_code == 422
    assert "acceptance_criteria" in resp.text
    assert client.get("/api/cards").json()["count"] == 0


def test_a_blank_acceptance_criterion_is_refused_on_patch(client):
    client.post("/api/cards", json={"card_key": "FCT-1", "title": "t"})

    resp = client.patch("/api/cards/FCT-1", json={"acceptance_criteria": [""]})

    assert resp.status_code == 422
    assert client.get("/api/cards/FCT-1").json()["acceptance_criteria"] == []


def test_real_criteria_still_round_trip_and_an_empty_list_is_still_legal(client):
    """The other direction, and the two shapes the rule must NOT refuse: an
    empty list (legal while planning - the contract says so) and entries with
    ordinary internal whitespace."""
    created = client.post(
        "/api/cards",
        json={"card_key": "FCT-3", "title": "t", "acceptance_criteria": ["  the board loads  "]},
    )
    assert created.status_code == 201
    # Trimmed, not rejected: `strip_whitespace` runs before the length bound.
    assert created.json()["acceptance_criteria"] == ["the board loads"]

    assert client.post("/api/cards", json={"title": "planning"}).status_code == 201
    empty = client.post(
        "/api/cards", json={"card_key": "FCT-5", "title": "t", "acceptance_criteria": []}
    )
    assert empty.status_code == 201
    assert empty.json()["acceptance_criteria"] == []
# ── unknown fields are rejected, not dropped (#322) ──────────────────────────


def test_a_server_owned_field_in_a_create_body_is_refused(client):
    """`tenant_id` is derived from the credential and appears in no request
    schema. Sending it used to be accepted with the key silently discarded, so
    the caller was told a write it did not get had landed."""
    resp = client.post("/api/cards", json={"title": "t", "tenant_id": "someone-else"})

    assert resp.status_code == 422
    assert "tenant_id" in resp.text
    # And the write did not half-happen: nothing was created from the rest.
    assert client.get("/api/cards").json()["count"] == 0


def test_a_misspelled_field_in_a_patch_body_is_refused(client):
    """The case that costs a human an afternoon: `assignees` for `assignee`,
    answered 200 with nothing changed.

    The body carries a VALID field beside the typo on purpose. `{"assignees":
    "olaf"}` alone would also be refused with `extra="ignore"` restored -- the
    minProperties guard below would catch the resulting empty patch -- so a
    single-key body cannot tell the two rules apart, and this test would pass
    while the rule it names was gone.
    """
    client.post("/api/cards", json={"card_key": "FCT-1", "title": "t"})

    resp = client.patch("/api/cards/FCT-1", json={"milestone": "v1", "assignees": "olaf"})

    assert resp.status_code == 422
    assert "assignees" in resp.text
    # Not half-applied either: the valid field beside the typo did not land.
    body = client.get("/api/cards/FCT-1").json()
    assert body["assignee"] is None
    assert body["milestone"] is None


def test_a_body_of_only_known_fields_still_works(client):
    """The other direction. `extra="forbid"` must reject the unknown key and
    nothing else -- every field the model declares still round-trips, including
    the two POST accepts that read like server-owned ones and are not."""
    resp = client.post(
        "/api/cards",
        json={
            "card_key": "FCT-7",
            "title": "t",
            "description": "body",
            "acceptance_criteria": ["a"],
            # `backlog`, not `ready`: creating straight into `ready` with a tier
            # IS the dispatch trigger, and this test is about the body shape.
            "status": "backlog",
            "priority": 3,
            "tier": "low",
            "assignee": "olaf",
            "milestone": "m1",
            "correlation_key": "corr-1",
            "issue_ref": "o/r#1",
            "repository_id": None,
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["correlation_key"] == "corr-1"
    assert client.patch("/api/cards/FCT-7", json={"status": "done"}).status_code == 200


def test_an_empty_patch_body_is_refused(client):
    """The contract's `minProperties: 1`. An empty PATCH used to be accepted and
    then apply nothing, because the route uses `exclude_unset`."""
    client.post("/api/cards", json={"card_key": "FCT-2", "title": "t"})

    resp = client.patch("/api/cards/FCT-2", json={})

    assert resp.status_code == 422
    assert "at least one field" in resp.text
    # A one-field PATCH is still a PATCH.
    assert client.patch("/api/cards/FCT-2", json={"priority": 9}).status_code == 200


# ── PATCH: move + reprioritise ───────────────────────────────────────────────


def test_patch_moves_and_reprioritises(client):
    client.post("/api/cards", json={"card_key": "FCT-7", "title": "t", "priority": 5})

    moved = client.patch("/api/cards/FCT-7", json={"status": "in_progress"})
    assert moved.json()["status"] == "in_progress"
    assert moved.json()["priority"] == 5  # untouched fields survive a partial PATCH

    reprioritised = client.patch("/api/cards/FCT-7", json={"priority": 1})
    assert reprioritised.json()["priority"] == 1
    assert reprioritised.json()["status"] == "in_progress"


def test_list_orders_by_priority_and_filters(client):
    client.post("/api/cards", json={"card_key": "A", "title": "a", "priority": 9})
    client.post(
        "/api/cards",
        json={
            "card_key": "B",
            "title": "b",
            "priority": 1,
            "status": "done",
            "milestone": "v1",
            "assignee": "olaf",
            "tier": "hard",
        },
    )
    assert [c["card_key"] for c in client.get("/api/cards").json()["cards"]] == ["B", "A"]

    for query in ("status=done", "milestone=v1", "assignee=olaf", "tier=hard"):
        found = client.get(f"/api/cards?{query}").json()
        assert [c["card_key"] for c in found["cards"]] == ["B"], query
    assert client.get("/api/cards?status=backlog").json()["count"] == 1


# ── tenant isolation ─────────────────────────────────────────────────────────


def test_tenant_cannot_read_or_modify_another_tenants_card(monkeypatch, cards, audit):
    client = _tenant_client(monkeypatch, cards, audit)
    acme = {"X-Tenant-Id": "acme"}
    globex = {"X-Tenant-Id": "globex"}

    client.post("/api/cards", json={"card_key": "FCT-1", "title": "acme secret"}, headers=acme)

    # Read: invisible in the list and a 404 on detail.
    assert client.get("/api/cards", headers=globex).json()["count"] == 0
    assert client.get("/api/cards/FCT-1", headers=globex).status_code == 404
    # Modify: neither PATCH nor DELETE can reach across the partition.
    patched = client.patch("/api/cards/FCT-1", json={"title": "pwned"}, headers=globex)
    assert patched.status_code == 404
    assert client.delete("/api/cards/FCT-1", headers=globex).status_code == 404

    # The owner still sees it, unmodified.
    owned = client.get("/api/cards/FCT-1", headers=acme)
    assert owned.status_code == 200
    assert owned.json()["title"] == "acme secret"
    assert owned.json()["tenant_id"] == "acme"


def test_same_card_key_may_exist_in_two_tenants(monkeypatch, cards, audit):
    client = _tenant_client(monkeypatch, cards, audit)
    for tenant in ("acme", "globex"):
        resp = client.post(
            "/api/cards",
            json={"card_key": "FCT-1", "title": f"{tenant} card"},
            headers={"X-Tenant-Id": tenant},
        )
        assert resp.status_code == 201, tenant
    assert (
        client.get("/api/cards/FCT-1", headers={"X-Tenant-Id": "acme"}).json()["title"]
        == "acme card"
    )


# ── write scope on every mutation ────────────────────────────────────────────


def _mutations(client):
    """(label, response) for each mutating endpoint, called with a read-only key."""
    ro = {"Authorization": "Bearer k_ro"}
    return [
        ("POST", client.post("/api/cards", json={"card_key": "X", "title": "t"}, headers=ro)),
        ("PATCH", client.patch("/api/cards/FCT-1", json={"status": "done"}, headers=ro)),
        ("DELETE", client.delete("/api/cards/FCT-1", headers=ro)),
    ]


def test_read_key_cannot_mutate_any_card_endpoint(client):
    client.post("/api/cards", json={"card_key": "FCT-1", "title": "t"})  # seeded in OPEN mode
    set_keys(RO)
    for label, resp in _mutations(client):
        assert resp.status_code == 403, f"{label} accepted a read-only key"
    # ...and the card was not touched.
    survived = client.get("/api/cards/FCT-1", headers={"Authorization": "Bearer k_ro"})
    assert survived.status_code == 200
    assert survived.json()["title"] == "t"


def test_write_key_may_mutate(client):
    set_keys(RW)
    rw = {"Authorization": "Bearer k_rw"}
    created = client.post("/api/cards", json={"card_key": "FCT-1", "title": "t"}, headers=rw)
    assert created.status_code == 201
    assert client.patch("/api/cards/FCT-1", json={"status": "done"}, headers=rw).status_code == 200
    assert client.delete("/api/cards/FCT-1", headers=rw).status_code == 200


def test_no_key_at_all_is_401(client):
    set_keys(RW)
    assert client.get("/api/cards").status_code == 401
    assert client.post("/api/cards", json={"card_key": "X", "title": "t"}).status_code == 401


# ── audit chain ──────────────────────────────────────────────────────────────


def test_every_mutation_appends_an_audit_entry(client, audit):
    assert audit.list() == []

    client.post("/api/cards", json={"card_key": "FCT-3", "title": "t"})
    client.patch("/api/cards/FCT-3", json={"status": "ready"})
    client.delete("/api/cards/FCT-3")

    entries = list(reversed(audit.list()))  # list() is newest-first
    assert [e.kind for e in entries] == ["create_card", "update_card", "delete_card"]
    assert {e.correlation_key for e in entries} == {"FCT-3"}
    assert all(e.target_service == "cfactory" and e.ok for e in entries)
    assert audit.verify() == []  # the HMAC chain is intact


def test_reads_and_failed_mutations_are_not_audited(client, audit):
    client.get("/api/cards")
    client.get("/api/cards/nope")
    client.patch("/api/cards/nope", json={"status": "done"})
    client.delete("/api/cards/nope")
    assert audit.list() == []

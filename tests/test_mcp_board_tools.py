"""MCP board tools over the planning cards (RFC-0019 Phase 2b, #302).

The RFC's design law is "every board action a human can take has a programmatic
equivalent" (§3.3). These tests hold that law to account on three axes:

* **Scope** — every board MUTATION is refused to a read-scoped key and accepted
  for a write-scoped one; the two read tools work with read alone.
* **Audit** — every mutation appends to the same tamper-evident HMAC chain the
  REST routes write, with the chain still verifying afterwards.
* **Equivalence** — a card created over MCP is visible over REST, and one
  created over REST is visible (and mutable) over MCP. That round trip IS the
  claim; it only holds because both transports call ``cfactory.card_ops``.

Both surfaces are wired to the SAME hermetic card + audit stores: REST through
``dependency_overrides``, MCP by patching the module-level seams it calls
directly (it has no Depends() to override).
"""

from __future__ import annotations

import json

import pytest
from cfactory import auth, config, mcp
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from fastapi.testclient import TestClient

# Not a credential: the HMAC anchor for the temp audit chain in these tests.
_TEST_HMAC = "mcp-board-test-hmac"

READER = "reader-key"
WRITER = "writer-key"

# The board mutations, as (tool, arguments) — the full set an agent can perform.
# Each must be write-scoped; every entry is exercised by the scope tests below.
MUTATIONS = [
    ("cfactory_create_card", {"title": "planned by an agent"}),
    ("cfactory_update_card", {"card_key": "FCT-1", "title": "renamed"}),
    ("cfactory_move_card", {"card_key": "FCT-1", "status": "in_progress"}),
    ("cfactory_reprioritise_card", {"card_key": "FCT-1", "priority": 3}),
    ("cfactory_delete_card", {"card_key": "FCT-1"}),
]


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)


@pytest.fixture
def client(cards, audit, monkeypatch):
    """One TestClient serving both surfaces over one pair of stores."""
    # MCP resolves its stores itself (no Depends), so patch the seams it calls.
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    # No legacy full-scope bearer: these tests are about the scoped keystore.
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.set_keys({READER: {"read"}, WRITER: {"read", "write"}})

    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    yield TestClient(app)
    auth.reset_keystore()


def call(client, token, name, arguments=None):
    """Raw JSON-RPC tools/call response."""
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )


def payload(client, token, name, arguments=None):
    """The decoded tool result, asserting the call itself was accepted."""
    resp = call(client, token, name, arguments)
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


def rest(client, method, path, **kw):
    return client.request(
        method, path, headers={"Authorization": f"Bearer {WRITER}"}, **kw
    )


# ── scope enforcement on every write tool ────────────────────────────────────


@pytest.mark.parametrize(("tool", "args"), MUTATIONS, ids=[t for t, _ in MUTATIONS])
def test_mutation_refused_to_read_scoped_key(client, tool, args):
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "seed"})

    refused = call(client, READER, tool, args)
    assert refused.status_code == 403
    assert "write" in refused.json()["detail"]

    # ...and the board is untouched: the seed card still reads exactly as seeded.
    seeded = payload(client, READER, "cfactory_get_card", {"card_key": "FCT-1"})
    assert seeded == {**seeded, "title": "seed", "status": "backlog", "priority": 0}


@pytest.mark.parametrize(("tool", "args"), MUTATIONS, ids=[t for t, _ in MUTATIONS])
def test_mutation_accepted_for_write_scoped_key(client, tool, args):
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "seed"})
    result = payload(client, WRITER, tool, args)
    assert "error" not in result, result


def test_read_tools_work_with_read_scope_alone(client):
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "seed"})

    assert payload(client, READER, "cfactory_list_cards")["count"] == 1
    assert payload(client, READER, "cfactory_get_card", {"card_key": "FCT-1"})["title"] == "seed"


# ── audit chain ──────────────────────────────────────────────────────────────


def test_every_mutation_appends_an_audit_entry(client, audit):
    assert audit.list() == []

    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-5", "title": "t"})
    payload(client, WRITER, "cfactory_move_card", {"card_key": "FCT-5", "status": "ready"})
    payload(client, WRITER, "cfactory_reprioritise_card", {"card_key": "FCT-5", "priority": 1})
    payload(client, WRITER, "cfactory_update_card", {"card_key": "FCT-5", "tier": "hard"})
    payload(client, WRITER, "cfactory_delete_card", {"card_key": "FCT-5"})

    entries = list(reversed(audit.list()))  # list() is newest-first
    assert [e.kind for e in entries] == [
        "create_card",
        "update_card",
        "update_card",
        "update_card",
        "delete_card",
    ]
    assert {e.correlation_key for e in entries} == {"FCT-5"}
    assert all(e.target_service == "cfactory" and e.ok for e in entries)
    # The actor is the presented MCP key, and the trail says the change came in
    # over /mcp rather than over the REST board.
    assert all(e.actor == WRITER and e.endpoint == "/mcp/FCT-5" for e in entries)
    assert audit.verify() == []  # the HMAC chain is intact


def test_reads_and_failed_mutations_are_not_audited(client, audit):
    payload(client, READER, "cfactory_list_cards")
    assert "error" in payload(client, WRITER, "cfactory_get_card", {"card_key": "nope"})
    assert "error" in payload(client, WRITER, "cfactory_move_card", {"card_key": "nope"})
    assert "error" in payload(client, WRITER, "cfactory_delete_card", {"card_key": "nope"})
    assert audit.list() == []


# ── programmatic equivalence: REST <-> MCP ───────────────────────────────────


def test_card_created_over_mcp_is_visible_over_rest(client):
    created = payload(
        client,
        WRITER,
        "cfactory_create_card",
        {
            "title": "agent-planned work",
            "acceptance_criteria": ["the board is one surface"],
            "tier": "medium",
            "milestone": "v1",
        },
    )
    assert created["card_key"] == "FCT-1"  # auto-assigned, same rule as REST

    over_rest = rest(client, "GET", "/api/cards/FCT-1")
    assert over_rest.status_code == 200
    # Byte-identical to what MCP reads back. Compared read-vs-read, not against
    # `created`: SQLite hands back naive datetimes, so a just-created card
    # renders its timestamps with a 'Z' and a re-read one without. That is a
    # pre-existing storage artifact of the Phase 1 cards table, and it is
    # identical on both transports — which is exactly the property under test.
    assert over_rest.json() == payload(client, WRITER, "cfactory_get_card", {"card_key": "FCT-1"})
    assert over_rest.json()["acceptance_criteria"] == ["the board is one surface"]
    assert over_rest.json()["milestone"] == "v1"

    listed = rest(client, "GET", "/api/cards?tier=medium").json()
    assert [c["card_key"] for c in listed["cards"]] == ["FCT-1"]


def test_card_created_over_rest_is_visible_and_mutable_over_mcp(client):
    rest(
        client,
        "POST",
        "/api/cards",
        json={"card_key": "FCT-9", "title": "human-planned work", "priority": 7},
    )

    seen = payload(client, WRITER, "cfactory_get_card", {"card_key": "FCT-9"})
    assert seen["title"] == "human-planned work"

    moved = payload(client, WRITER, "cfactory_move_card", {"card_key": "FCT-9", "status": "done"})
    assert moved["status"] == "done"
    assert moved["priority"] == 7  # a move leaves every other field alone

    # The human's cockpit sees the agent's move immediately.
    assert rest(client, "GET", "/api/cards/FCT-9").json()["status"] == "done"

    payload(client, WRITER, "cfactory_delete_card", {"card_key": "FCT-9"})
    assert rest(client, "GET", "/api/cards/FCT-9").status_code == 404


def test_mcp_and_rest_agree_on_the_backlog_listing(client):
    payload(client, WRITER, "cfactory_create_card", {"card_key": "A", "title": "a", "priority": 9})
    rest(client, "POST", "/api/cards", json={"card_key": "B", "title": "b", "priority": 1})

    via_mcp = payload(client, WRITER, "cfactory_list_cards")
    via_rest = rest(client, "GET", "/api/cards").json()
    assert via_mcp == via_rest
    assert [c["card_key"] for c in via_mcp["cards"]] == ["B", "A"]  # priority order


# ── argument validation ──────────────────────────────────────────────────────


def test_invalid_status_is_reported_not_applied(client):
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "t"})

    result = payload(client, WRITER, "cfactory_move_card", {"card_key": "FCT-1", "status": "wat"})
    assert result["error"] == "invalid arguments"
    assert payload(client, WRITER, "cfactory_get_card", {"card_key": "FCT-1"})["status"] == "backlog"


def test_duplicate_card_key_is_reported(client):
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "one"})
    dup = payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "again"})
    assert "already exists" in dup["error"]


def test_every_board_tool_declares_a_scope(client):
    """No board tool may rely on the unregistered-defaults-to-WRITE fallback —
    the read tools would be silently write-gated if one were forgotten."""
    for tool in mcp.BOARD_TOOLS:
        assert tool["name"] in mcp.TOOL_SCOPES, tool["name"]
        assert tool["name"] in mcp._TOOL_HANDLERS, tool["name"]

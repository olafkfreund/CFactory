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
* **Intake equivalence (Phase 3 x Phase 2b)** — promoting a card to ``ready``
  with a tier over MCP dispatches it into the factory *identically* to the same
  promotion over ``PATCH``: same door, same payload, same join, same
  idempotency, same fail-safe. This is the sharpest form of the law — the two
  surfaces agreeing about the board while disagreeing about whether the work
  actually entered the pipeline would be the worst kind of false parity.

Both surfaces are wired to the SAME hermetic card + audit stores and the SAME
mock upstream: REST through ``dependency_overrides``, MCP by patching the
module-level seams it calls directly (it has no Depends() to override).
"""

from __future__ import annotations

import json

import httpx
import pytest
from cfactory import auth, card_intake, config, mcp
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from cfactory.config import Settings
from fastapi.testclient import TestClient

from cfactory.api_deps import action_transport_dep  # isort: skip

# Not a credential: the HMAC anchor for the temp audit chain in these tests.
_TEST_HMAC = "mcp-board-test-hmac"

AIF_INTAKE = card_intake.AIFACTORY_INTAKE_ENDPOINT
PF_INTAKE = card_intake.PFACTORY_INTAKE_ENDPOINT

READER = "reader-key"
WRITER = "writer-key"

# The board mutations, as (tool, arguments) — the full set an agent can perform.
# Each must be write-scoped; every entry is exercised by the scope tests below.
MUTATIONS = [
    ("cfactory_create_card", {"title": "planned by an agent"}),
    ("cfactory_update_card", {"card_key": "FCT-1", "title": "renamed"}),
    ("cfactory_move_card", {"card_key": "FCT-1", "status": "in_progress"}),
    ("cfactory_reprioritise_card", {"card_key": "FCT-1", "priority": 3}),
    # Phase 6. Inert here (no CFACTORY_GITHUB_TOKEN in these Settings), which is
    # the point: it must still be refused to a read-scoped key.
    ("cfactory_sync_card_github", {"card_key": "FCT-1"}),
    # Phase 7 stage actions (RFC-0020 §3.7). These dispatch work into the factory,
    # so a read-scoped key must be unable to reach any of them.
    ("cfactory_plan_card", {"card_key": "FCT-1"}),
    ("cfactory_code_card", {"card_key": "FCT-1"}),
    ("cfactory_test_card", {"card_key": "FCT-1"}),
    ("cfactory_run_card", {"card_key": "FCT-1"}),
    ("cfactory_delete_card", {"card_key": "FCT-1"}),
]


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)


class Upstream:
    """Records every intake POST and answers with a canned response."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[tuple[str, dict]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(self.status_code, json={"task_id": "task-7"})

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def client(cards, audit, upstream, monkeypatch):
    """One TestClient serving both surfaces over one pair of stores and one
    mock upstream — so an intake dispatch is observable whichever surface
    triggered it, and neither surface can reach the real network."""
    # MCP resolves its own collaborators (no Depends), so patch the seams it calls.
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", lambda: upstream.transport)
    # An intake project is configured, so a low/medium card has a build target.
    monkeypatch.setattr(card_intake, "get_settings", lambda: Settings(intake_project_id="proj-1"))
    # No legacy full-scope bearer: these tests are about the scoped keystore.
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    config.reset_settings()
    auth.set_keys({READER: {"read"}, WRITER: {"read", "write"}})

    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = lambda: upstream.transport
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
    # Seeded WITH a tier so the Phase 7 stage actions have a payload to build; the
    # card stays in `backlog`, so seeding still dispatches nothing.
    payload(
        client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "seed", "tier": "low"}
    )
    result = payload(client, WRITER, tool, args)
    if tool == "cfactory_test_card":
        # The one mutation that cannot succeed on a fresh card: there is nothing
        # built to verify. That is a domain refusal, not an authorization one —
        # ``payload`` already asserted the write scope got through.
        assert result["reason"] == "no_build_to_verify", result
    else:
        assert "error" not in result, result


def test_read_tools_work_with_read_scope_alone(client):
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "title": "seed"})

    assert payload(client, READER, "cfactory_list_cards")["count"] == 1
    assert payload(client, READER, "cfactory_get_card", {"card_key": "FCT-1"})["title"] == "seed"


# ── audit chain ──────────────────────────────────────────────────────────────


def test_every_mutation_appends_an_audit_entry(client, audit, upstream):
    assert audit.list() == []

    # Deliberately never `ready` + tier together, so this covers the CRUD audit
    # entries alone; the extra `dispatch_card` entry an intake adds is asserted
    # in test_mcp_dispatch_appends_its_own_audit_entry below.
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-5", "title": "t"})
    payload(client, WRITER, "cfactory_move_card", {"card_key": "FCT-5", "status": "ready"})
    payload(client, WRITER, "cfactory_reprioritise_card", {"card_key": "FCT-5", "priority": 1})
    payload(client, WRITER, "cfactory_update_card", {"card_key": "FCT-5", "title": "renamed"})
    payload(client, WRITER, "cfactory_delete_card", {"card_key": "FCT-5"})
    assert upstream.calls == []  # a ready card with no tier is not an intake event

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
    # The actor REFERENCES the presented MCP key without being it (#251 — it used
    # to be the key verbatim), and the trail says the change came in over /mcp
    # rather than over the REST board.
    assert all(e.actor == auth.key_actor(WRITER) and e.endpoint == "/mcp/FCT-5" for e in entries)
    assert all(WRITER not in e.actor for e in entries)
    assert audit.verify() == []  # the HMAC chain is intact


def test_reads_and_failed_mutations_are_not_audited(client, audit):
    payload(client, READER, "cfactory_list_cards")
    assert "error" in payload(client, WRITER, "cfactory_get_card", {"card_key": "nope"})
    assert "error" in payload(client, WRITER, "cfactory_move_card", {"card_key": "nope"})
    assert "error" in payload(client, WRITER, "cfactory_delete_card", {"card_key": "nope"})
    assert audit.list() == []


def test_an_unknown_argument_is_refused_over_mcp_too(client):
    """#322 reaches the agent surface, not only REST.

    ``_tool_create_card`` builds a ``CardCreate`` straight from the tool
    arguments and nothing validates them against the tool's own ``inputSchema``
    first, so ``extra="forbid"`` is what stands between an agent's typo and a
    write it is told succeeded.
    """
    body = payload(client, WRITER, "cfactory_create_card", {"title": "t", "tenant_id": "other"})

    assert body["error"] == "invalid arguments"
    assert any(e["loc"] == ["tenant_id"] for e in body["details"]), body
    assert payload(client, WRITER, "cfactory_list_cards")["count"] == 0


def test_a_tool_call_that_would_change_nothing_says_so_and_does_not_500(client):
    """``cfactory_move_card`` with no ``status`` reduces to an empty CardUpdate.

    The assertion that matters is the FIRST one: rendering this used to raise
    ``TypeError: Object of type ValueError is not JSON serializable`` out of the
    handler, because pydantic puts the raised exception object in the error's
    ``ctx`` and ``_TOOL_ERRORS`` passed ``ctx`` to the JSON encoder. An agent got
    a 500 where it should have got a sentence, and that is true of every
    raising validator, not only this one.
    """
    resp = call(client, WRITER, "cfactory_move_card", {"card_key": "FCT-1"})

    assert resp.status_code == 200, resp.text
    body = json.loads(resp.json()["result"]["content"][0]["text"])
    assert body["error"] == "invalid arguments"
    assert "at least one field" in json.dumps(body["details"])


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


# ── intake equivalence: MCP promotion dispatches like a PATCH (Phase 3) ──────
#
# Phase 3 made "ready + tier" the intake trigger on the REST routes. Because the
# dispatch now lives in the SHARED card_ops path rather than in the route, an
# agent promoting a card over MCP must enter the factory identically. If these
# fail, the board agrees across surfaces while the pipeline does not — the exact
# false parity RFC-0019 §3.3 exists to rule out.

PLAN = {"title": "Ship the widget", "acceptance_criteria": ["AC#1: it ships"]}


def _seed_pair(client, tier):
    """The same card, planned once over each surface, ready to be promoted."""
    payload(client, WRITER, "cfactory_create_card", {"card_key": "VIA-MCP", "tier": tier, **PLAN})
    rest(client, "POST", "/api/cards", json={"card_key": "VIA-REST", "tier": tier, **PLAN})


@pytest.mark.parametrize(
    ("tier", "door"),
    [("low", AIF_INTAKE), ("medium", AIF_INTAKE), ("hard", PF_INTAKE)],
)
def test_mcp_promotion_dispatches_through_the_same_door_as_a_patch(client, upstream, tier, door):
    """Same tier routing (RFC-0011 §3), same payload, same resulting card."""
    _seed_pair(client, tier)
    assert upstream.calls == []  # planning a card does not dispatch it

    via_mcp = payload(client, WRITER, "cfactory_move_card", {"card_key": "VIA-MCP", "status": "ready"})
    via_rest = rest(client, "PATCH", "/api/cards/VIA-REST", json={"status": "ready"}).json()

    # One dispatch each, both through the tier's documented door.
    assert upstream.paths == [door, door]
    # ...carrying an identical body: the card is the brief, and these two cards
    # differ only in the key that is never sent upstream.
    assert upstream.calls[0][1] == upstream.calls[1][1]

    # ...leaving both cards in the same joined state.
    assert via_mcp["correlation_key"] == via_rest["correlation_key"] == "task-7"
    assert via_mcp["status"] == via_rest["status"] == "in_progress"


def test_mcp_promotion_is_idempotent_exactly_as_a_patch_is(client, upstream):
    """A re-promotion of an already-joined card does not build it twice —
    over either surface, and across them (correlation_key IS the guard)."""
    _seed_pair(client, "low")
    payload(client, WRITER, "cfactory_move_card", {"card_key": "VIA-MCP", "status": "ready"})
    rest(client, "PATCH", "/api/cards/VIA-REST", json={"status": "ready"})
    assert len(upstream.calls) == 2

    # Re-promote each card over BOTH surfaces: four writes, zero new dispatches.
    payload(client, WRITER, "cfactory_move_card", {"card_key": "VIA-MCP", "status": "ready"})
    payload(client, WRITER, "cfactory_move_card", {"card_key": "VIA-REST", "status": "ready"})
    rest(client, "PATCH", "/api/cards/VIA-MCP", json={"status": "ready"})
    rest(client, "PATCH", "/api/cards/VIA-REST", json={"status": "ready"})

    assert len(upstream.calls) == 2, "a joined card was dispatched a second time"
    assert payload(client, WRITER, "cfactory_get_card", {"card_key": "VIA-MCP"})[
        "correlation_key"
    ] == "task-7"


def test_card_created_ready_over_mcp_dispatches_immediately(client, upstream):
    """The trigger is the card's STATE, not which verb produced it — so a create
    straight into ready dispatches, over MCP just as over POST."""
    payload(
        client, WRITER, "cfactory_create_card", {"status": "ready", "tier": "low", **PLAN}
    )
    assert upstream.paths == [AIF_INTAKE]


def test_mcp_dispatch_appends_its_own_audit_entry(client, audit, upstream):
    """The dispatch is audited separately from the card write, attributed to the
    UPSTREAM factory, and the chain still verifies."""
    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "tier": "low", **PLAN})
    payload(client, WRITER, "cfactory_move_card", {"card_key": "FCT-1", "status": "ready"})

    entries = list(reversed(audit.list()))
    assert [e.kind for e in entries] == ["create_card", "update_card", "dispatch_card"]
    dispatched = entries[-1]
    assert dispatched.target_service == "aifactory"  # not cfactory: it left the building
    assert dispatched.correlation_key == "task-7"
    assert dispatched.actor == auth.key_actor(WRITER) and dispatched.endpoint == "/mcp/FCT-1"
    assert dispatched.ok is True
    assert audit.verify() == []


def test_failed_dispatch_over_mcp_blocks_the_card_and_is_surfaced(client, audit, upstream):
    """Fail-safe, same as REST: a card that could NOT enter the factory is
    blocked rather than left looking ready, the failure is audited ok=False, and
    the tool call is NOT a JSON-RPC internal error."""
    upstream.status_code = 500

    payload(client, WRITER, "cfactory_create_card", {"card_key": "FCT-1", "tier": "low", **PLAN})
    resp = call(client, WRITER, "cfactory_move_card", {"card_key": "FCT-1", "status": "ready"})

    assert resp.status_code == 200
    assert "error" not in resp.json(), "a failing upstream must not become a JSON-RPC error"

    blocked = payload(client, WRITER, "cfactory_get_card", {"card_key": "FCT-1"})
    assert blocked["status"] == "blocked"
    assert blocked["correlation_key"] is None  # never joined, so still re-dispatchable

    failed = audit.list()[0]  # newest-first
    assert failed.kind == "dispatch_card"
    assert failed.ok is False
    assert audit.verify() == []


def test_every_board_tool_declares_a_scope(client):
    """No board tool may rely on the unregistered-defaults-to-WRITE fallback —
    the read tools would be silently write-gated if one were forgotten."""
    for tool in mcp.BOARD_TOOLS:
        assert tool["name"] in mcp.TOOL_SCOPES, tool["name"]
        assert tool["name"] in mcp._TOOL_HANDLERS, tool["name"]

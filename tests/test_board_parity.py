"""Programmatic-equivalence parity check (RFC-0019 Phase 5, §3.3, #302).

The design law: **every board action a human can take has an identical REST +
MCP equivalent.** This module is the CI check that keeps the law true rather
than aspirational — it fails when someone adds one surface and forgets the
other.

Parity is asserted at the level of board OPERATIONS, not of names: REST exposes
one ``PATCH /api/cards/{card_key}`` that covers update, move AND reprioritise,
while MCP splits those into three tools. That is deliberate — different
ergonomics for the same capability — so one REST route may back several MCP
tools, and a 1:1 name match would be the wrong test.

Both surfaces are ENUMERATED LIVE (the app's own OpenAPI document, the real
``MCP_TOOLS`` catalog) rather than hand-copied here, and compared against
:data:`BOARD_OPERATIONS` in both directions: an unclaimed route fails, an
unclaimed tool fails, and a stale map entry fails too.
"""

from __future__ import annotations

from dataclasses import dataclass

from cfactory.app import create_app
from cfactory.auth import READ, WRITE
from cfactory.mcp import MCP_TOOLS, TOOL_SCOPES

# The prefix every board REST route lives under, and the substring that marks a
# tool as a board tool. Both are how a NEW surface gets noticed: add
# ``POST /api/cards/{card_key}/archive`` or ``cfactory_archive_card`` and the
# orphan checks below see it without anyone updating this file.
CARD_ROUTE_PREFIX = "/api/cards"
CARD_TOOL_MARKER = "card"


@dataclass(frozen=True)
class BoardOperation:
    """One board capability and the two ways to invoke it."""

    name: str
    method: str
    path: str
    tool: str

    @property
    def mutates(self) -> bool:
        return self.method != "GET"


# The operation map. Adding a board capability means adding a line here WITH
# both surfaces filled in — which is the point: there is nowhere to declare an
# operation that only one transport can perform.
BOARD_OPERATIONS: tuple[BoardOperation, ...] = (
    BoardOperation("list", "GET", "/api/cards", "cfactory_list_cards"),
    BoardOperation("get", "GET", "/api/cards/{card_key}", "cfactory_get_card"),
    BoardOperation("create", "POST", "/api/cards", "cfactory_create_card"),
    BoardOperation("update", "PATCH", "/api/cards/{card_key}", "cfactory_update_card"),
    BoardOperation("move", "PATCH", "/api/cards/{card_key}", "cfactory_move_card"),
    BoardOperation("reprioritise", "PATCH", "/api/cards/{card_key}", "cfactory_reprioritise_card"),
    BoardOperation("delete", "DELETE", "/api/cards/{card_key}", "cfactory_delete_card"),
)


def _live_rest_routes() -> set[tuple[str, str]]:
    """Every (method, path) the app actually serves under the board.

    Read off the generated OpenAPI document rather than the router objects: it
    is the same published contract an agent discovers over ``/openapi.json``
    (RFC-0019 §3.3), so a route that is served but undocumented is not parity
    either.
    """
    return {
        (method.upper(), path)
        for path, ops in create_app().openapi()["paths"].items()
        if path.startswith(CARD_ROUTE_PREFIX)
        for method in ops
    }


def _live_mcp_tools() -> set[str]:
    """Every board tool the MCP catalog actually advertises."""
    return {t["name"] for t in MCP_TOOLS if CARD_TOOL_MARKER in t["name"]}


def test_every_rest_route_has_an_mcp_twin() -> None:
    claimed = {(op.method, op.path) for op in BOARD_OPERATIONS}
    live = _live_rest_routes()
    assert live == claimed, (
        "board REST/MCP parity broken (RFC-0019 §3.3).\n"
        f"  routes with no operation (add an MCP tool + a map line): {live - claimed}\n"
        f"  operations whose route no longer exists: {claimed - live}"
    )


def test_every_mcp_tool_has_a_rest_twin() -> None:
    claimed = {op.tool for op in BOARD_OPERATIONS}
    live = _live_mcp_tools()
    assert live == claimed, (
        "board REST/MCP parity broken (RFC-0019 §3.3).\n"
        f"  tools with no operation (add a REST route + a map line): {live - claimed}\n"
        f"  operations whose tool no longer exists: {claimed - live}"
    )


def test_scopes_match_the_operation() -> None:
    """A mutating operation declares WRITE; a read declares READ.

    The HTTP method is the source of truth, so the two can never be declared
    inconsistently — a read-scoped key must be able to enumerate the backlog and
    unable to change it, over either transport.
    """
    expected = {op.tool: WRITE if op.mutates else READ for op in BOARD_OPERATIONS}
    actual = {tool: TOOL_SCOPES.get(tool) for tool in expected}
    assert actual == expected

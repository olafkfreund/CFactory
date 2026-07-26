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

# The prefixes every board REST route lives under, and the substrings that mark
# a tool as a board tool. Both are how a NEW surface gets noticed: add
# ``POST /api/cards/{card_key}/archive`` or ``cfactory_archive_card`` and the
# orphan checks below see it without anyone updating this file.
#
# ``/api/tenants`` + ``git_config`` joined the list with RFC-0020 §3.3: the
# tenant git configuration is a board resource — it decides which host and which
# project every card action talks to — so its mutations are under the same parity
# law, and a REST route added there without an MCP twin fails this build.
#
# ``git_`` rather than a list of the individual git nouns since RFC-0020 phase 8:
# the surface grew connections and repositories, and a marker per noun is a marker
# somebody forgets to add. Any future ``cfactory_*_git_*`` tool is caught.
BOARD_ROUTE_PREFIXES = ("/api/cards", "/api/tenants")
BOARD_TOOL_MARKERS = ("card", "git_")


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
    BoardOperation(
        "sync_github",
        "POST",
        "/api/cards/{card_key}/sync-github",
        "cfactory_sync_card_github",
    ),
    BoardOperation("import", "POST", "/api/cards/import", "cfactory_import_cards"),
    BoardOperation("delete", "DELETE", "/api/cards/{card_key}", "cfactory_delete_card"),
    # Explicit stage actions (RFC-0020 §3.7, #369). Unlike update/move/reprioritise
    # these are NOT one route behind several tools: each stage is its own route and
    # its own tool, because "plan this" and "test this" are different capabilities
    # with different preconditions, not two ergonomics for one call.
    BoardOperation("plan", "POST", "/api/cards/{card_key}/actions/plan", "cfactory_plan_card"),
    BoardOperation("code", "POST", "/api/cards/{card_key}/actions/code", "cfactory_code_card"),
    BoardOperation("test", "POST", "/api/cards/{card_key}/actions/test", "cfactory_test_card"),
    BoardOperation("run", "POST", "/api/cards/{card_key}/actions/run", "cfactory_run_card"),
    # Tenant git configuration (RFC-0020 §3.3, #363). One route per tool here:
    # reading a configuration, replacing it and proving it reaches its project
    # are three different capabilities, not three ergonomics for one call.
    BoardOperation(
        "get_git_config",
        "GET",
        "/api/tenants/{tenant}/git-config",
        "cfactory_get_git_config",
    ),
    BoardOperation(
        "set_git_config",
        "PUT",
        "/api/tenants/{tenant}/git-config",
        "cfactory_set_git_config",
    ),
    BoardOperation(
        "verify_git_config",
        "POST",
        "/api/tenants/{tenant}/git-config:verify",
        "cfactory_verify_git_config",
    ),
    # Tenant git credential (RFC-0020 §3.4, #364). Two operations, both writes,
    # and no read: the credential is write-only, so there is no GET to pair a
    # read tool with. Whether one exists rides on get_git_config's masked block.
    BoardOperation(
        "set_git_credential",
        "PUT",
        "/api/tenants/{tenant}/git-credential",
        "cfactory_set_git_credential",
    ),
    BoardOperation(
        "delete_git_credential",
        "DELETE",
        "/api/tenants/{tenant}/git-credential",
        "cfactory_delete_git_credential",
    ),
    # Git connections and repositories (RFC-0020 §3.3 phase 8, #373). Two levels,
    # full CRUD on each, and every mutation twinned: a tenant with a GitHub
    # connection and a GitLab connection, each with several repositories, must be
    # as configurable by an agent as it is by a human in the panel.
    BoardOperation(
        "list_git_connections",
        "GET",
        "/api/tenants/{tenant}/git-connections",
        "cfactory_list_git_connections",
    ),
    BoardOperation(
        "create_git_connection",
        "POST",
        "/api/tenants/{tenant}/git-connections",
        "cfactory_create_git_connection",
    ),
    BoardOperation(
        "update_git_connection",
        "PATCH",
        "/api/tenants/{tenant}/git-connections/{connection_id}",
        "cfactory_update_git_connection",
    ),
    BoardOperation(
        "delete_git_connection",
        "DELETE",
        "/api/tenants/{tenant}/git-connections/{connection_id}",
        "cfactory_delete_git_connection",
    ),
    BoardOperation(
        "verify_git_connection",
        "POST",
        "/api/tenants/{tenant}/git-connections/{connection_id}:verify",
        "cfactory_verify_git_connection",
    ),
    BoardOperation(
        "set_git_connection_credential",
        "PUT",
        "/api/tenants/{tenant}/git-connections/{connection_id}/credential",
        "cfactory_set_git_connection_credential",
    ),
    BoardOperation(
        "delete_git_connection_credential",
        "DELETE",
        "/api/tenants/{tenant}/git-connections/{connection_id}/credential",
        "cfactory_delete_git_connection_credential",
    ),
    BoardOperation(
        "list_git_repositories",
        "GET",
        "/api/tenants/{tenant}/git-repositories",
        "cfactory_list_git_repositories",
    ),
    BoardOperation(
        "create_git_repository",
        "POST",
        "/api/tenants/{tenant}/git-connections/{connection_id}/repositories",
        "cfactory_create_git_repository",
    ),
    BoardOperation(
        "update_git_repository",
        "PATCH",
        "/api/tenants/{tenant}/git-repositories/{repository_id}",
        "cfactory_update_git_repository",
    ),
    BoardOperation(
        "delete_git_repository",
        "DELETE",
        "/api/tenants/{tenant}/git-repositories/{repository_id}",
        "cfactory_delete_git_repository",
    ),
    BoardOperation(
        "set_default_git_repository",
        "POST",
        "/api/tenants/{tenant}/git-repositories/{repository_id}:default",
        "cfactory_set_default_git_repository",
    ),
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
        if path.startswith(BOARD_ROUTE_PREFIXES)
        for method in ops
    }


def _live_mcp_tools() -> set[str]:
    """Every board tool the MCP catalog actually advertises."""
    return {
        t["name"] for t in MCP_TOOLS if any(m in t["name"] for m in BOARD_TOOL_MARKERS)
    }


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

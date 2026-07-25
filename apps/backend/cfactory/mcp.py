"""CFactory MCP server — the single PARR-pipeline visibility surface.

Implements a minimal POST-only JSON-RPC 2.0 MCP transport at ``POST /mcp`` so an
external agent (Claude Code, the ``/parr-run`` conductor) can watch the whole
plan -> code -> test pipeline from one place. CFactory already correlates every
factory's state by ``correlation_key`` (the GitHub issue number); this exposes
that aggregation read-only as MCP tools instead of forcing the agent to poll
three separate factories.

Read-only for the PARR pipeline view; the RFC-0019 Phase 2b **board tools** add
the write half, so an agent manages the planning backlog exactly as a human does
in the cockpit (§3.3, programmatic equivalence). Those tools do not reimplement
anything: they call :mod:`cfactory.card_ops`, the same store + audit path the
REST routes use, so a card created over MCP is byte-identical to one created
over ``POST /api/cards``.

Auth (three credentials, in precedence order):

1. ``CFACTORY_MCP_SECRET`` — the LEGACY full-scope bearer. Live in prod; a caller
   presenting it holds read AND write, exactly as before.
2. ``CFACTORY_API_KEYS`` — the same scoped keystore that gates ``/api`` and
   ``/connect`` (``"<key>:read;<key2>:read,write"``). A key may call a tool only
   if it carries that tool's declared scope. This is the additive part.
3. Nothing configured — DENIED. It used to be open; hanging write tools off a
   fail-open surface is how a backlog gets mutated by anyone. Set
   ``CFACTORY_MCP_DEV_OPEN=true`` for local dev to opt back into open mode.

Unregistered tool names default to WRITE, so a tool added without a scope entry
fails closed rather than inheriting read.

Configure in an MCP client:
    {
      "mcpServers": {
        "cfactory": {
          "type": "http",
          "url": "${CFACTORY_URL}/mcp",
          "headers": {"Authorization": "Bearer ${CFACTORY_MCP_TOKEN}"}
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import card_ops
from .api_deps import cards_store_dep
from .audit import get_audit_store
from .auth import READ, WRITE, extract_key, get_keystore, secret_matches
from .card_ops import CardNotFoundError
from .cards import CardCreate, CardStore, CardUpdate, DuplicateCardKeyError
from .config import get_settings
from .copilot.anomalies import detect_anomalies
from .copilot.tools import rollups as compute_rollups
from .copilot.tools import summarize_timeline
from .enterprise import identity_dep
from .store import get_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["CFactory MCP"])

# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "cfactory_list_workitems",
        "description": (
            "List every unit of work threaded across the PARR pipeline, with each "
            "factory's current status. One row per correlation_key (GitHub issue #). "
            "Use this to see the whole pipeline at a glance."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_get_workitem",
        "description": (
            "Get the full cross-factory state for one unit of work: PFactory (plan), "
            "AIFactory (code), TFactory (test) slices plus the event timeline."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["correlation_key"],
            "properties": {
                "correlation_key": {
                    "type": "string",
                    "description": "The GitHub issue number / shared key threading the work.",
                }
            },
        },
    },
    {
        "name": "cfactory_get_timeline",
        "description": (
            "Get the ordered completion-event timeline for one unit of work — the "
            "sequence of plan/code/test transitions across factories."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["correlation_key"],
            "properties": {
                "correlation_key": {"type": "string"},
            },
        },
    },
    {
        "name": "cfactory_get_rollups",
        "description": (
            "Aggregate counts across the pipeline (how many units are planning / "
            "coding / testing / done / stuck) — the cockpit's column totals."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_get_anomalies",
        "description": (
            "Detected anomalies needing attention: stuck tasks, failed phases, "
            "handbacks awaiting a human, stalled hand-offs. Poll this to know when "
            "a unit of work needs the coder's attention."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Board tools (RFC-0019 Phase 2b) — the planning backlog, read AND write
# ---------------------------------------------------------------------------

_CARD_KEY_PROP = {"type": "string", "description": "Stable card id, e.g. 'FCT-42'."}

# The fields an agent may edit on an existing card. Declared once and spliced
# into both the create and the update schema so the two can never drift.
_CARD_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "description": "One-line statement of intent."},
    "acceptance_criteria": {
        "type": "array",
        "items": {"type": "string"},
        "description": "What must be true for the card to be done.",
    },
    "tier": {
        "type": "string",
        "enum": ["low", "medium", "hard"],
        "description": "RFC-0011 difficulty tier, deciding which intake path builds it.",
    },
    "assignee": {
        "type": "string",
        "description": "Owner — a human handle or a factory runtime ('aifactory').",
    },
    "milestone": {"type": "string", "description": "Release / grouping this card belongs to."},
}

_STATUS_PROP = {
    "type": "string",
    "enum": ["backlog", "ready", "in_progress", "blocked", "done"],
    "description": "The board column the card sits in.",
}

_PRIORITY_PROP = {
    "type": "integer",
    "description": "Lower sorts first, so 0 is the top of the backlog.",
}

BOARD_TOOLS: list[dict[str, Any]] = [
    {
        "name": "cfactory_list_cards",
        "description": (
            "List / search the planning backlog — this tenant's cards, highest priority "
            "first. Filter to one board column, release, owner, or difficulty tier. This "
            "is the same backlog a human sees in the cockpit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": _STATUS_PROP,
                "milestone": {"type": "string"},
                "assignee": {"type": "string"},
                "tier": {"type": "string", "enum": ["low", "medium", "hard"]},
            },
        },
    },
    {
        "name": "cfactory_get_card",
        "description": (
            "Get one planning card in full: title, acceptance criteria, status, priority, "
            "tier, assignee, milestone, and the correlation_key joining it to the PARR "
            "pipeline once it has entered the factory."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    {
        "name": "cfactory_create_card",
        "description": (
            "Add a card to the planning backlog. Omit card_key to have the next "
            "'FCT-<n>' assigned. Use this to plan work into the board rather than "
            "filing a bare GitHub issue."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "card_key": {
                    "type": "string",
                    "description": "Optional explicit id; auto-assigned when omitted.",
                },
                **_CARD_FIELDS,
                "status": _STATUS_PROP,
                "priority": _PRIORITY_PROP,
                "correlation_key": {
                    "type": "string",
                    "description": "RFC-0001 correlation key, once the card enters the factory.",
                },
            },
        },
    },
    {
        "name": "cfactory_update_card",
        "description": (
            "Edit an existing card's content: title, acceptance criteria, tier, assignee, "
            "milestone. Only the fields you pass are changed. To change the board column "
            "use cfactory_move_card; to change ordering use cfactory_reprioritise_card."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP, **_CARD_FIELDS},
        },
    },
    {
        "name": "cfactory_move_card",
        "description": (
            "Move a card between board columns (backlog / ready / in_progress / blocked / "
            "done) — the programmatic equivalent of dragging it in the cockpit."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key", "status"],
            "properties": {"card_key": _CARD_KEY_PROP, "status": _STATUS_PROP},
        },
    },
    {
        "name": "cfactory_reprioritise_card",
        "description": (
            "Change a card's priority, which is what orders the backlog (lower first)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key", "priority"],
            "properties": {"card_key": _CARD_KEY_PROP, "priority": _PRIORITY_PROP},
        },
    },
    {
        "name": "cfactory_delete_card",
        "description": "Remove a card from the planning backlog for good.",
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
]

MCP_TOOLS += BOARD_TOOLS


# The scope each tool requires. Kept beside the catalog rather than inside the
# tool dicts so ``tools/list`` stays byte-for-byte the MCP shape clients already
# parse. Reads declare READ; every board MUTATION declares WRITE, so a read-only
# key can enumerate and inspect the backlog but never change it.
TOOL_SCOPES: dict[str, str] = {
    "cfactory_list_workitems": READ,
    "cfactory_get_workitem": READ,
    "cfactory_get_timeline": READ,
    "cfactory_get_rollups": READ,
    "cfactory_get_anomalies": READ,
    "cfactory_list_cards": READ,
    "cfactory_get_card": READ,
    "cfactory_create_card": WRITE,
    "cfactory_update_card": WRITE,
    "cfactory_move_card": WRITE,
    "cfactory_reprioritise_card": WRITE,
    "cfactory_delete_card": WRITE,
}


# ---------------------------------------------------------------------------
# Auth — scope model (RFC-0019 Phase 2a)
# ---------------------------------------------------------------------------

FULL_SCOPE = frozenset({READ, WRITE})


def _authenticate(request: Request) -> frozenset[str]:
    """Return the scopes the caller holds, or raise 401.

    Fails CLOSED: with no credential configured at all the request is denied
    unless ``CFACTORY_MCP_DEV_OPEN`` is explicitly set.
    """
    settings = get_settings()
    token = extract_key(request.headers.get("authorization"), request.headers.get("x-api-key"))

    # 1. Legacy full-scope bearer — unchanged behaviour for existing prod clients.
    if settings.mcp_secret and secret_matches(token, settings.mcp_secret):
        return FULL_SCOPE

    # 2. Scoped API keys — the same keystore that gates /api and /connect.
    keystore = get_keystore()
    if keystore.configured:
        scopes = keystore.scopes_for(token)
        if scopes is not None:
            return frozenset(scopes)

    # 3. Nothing matched. If something IS configured this is a bad token;
    #    if nothing is configured it is an unconfigured server (deny, not open).
    if settings.mcp_secret or keystore.configured:
        raise HTTPException(status_code=401, detail="Invalid MCP token")
    if settings.mcp_dev_open:
        return FULL_SCOPE
    raise HTTPException(
        status_code=401,
        detail=(
            "MCP is not configured: set CFACTORY_MCP_SECRET or CFACTORY_API_KEYS "
            "(or CFACTORY_MCP_DEV_OPEN=true for local dev)"
        ),
    )


def _require_tool_scope(tool_name: str, granted: frozenset[str]) -> None:
    """Raise 403 unless ``granted`` covers the scope ``tool_name`` declares.

    Unknown tools require WRITE — a tool added without a TOOL_SCOPES entry fails
    closed instead of silently inheriting read access.
    """
    required = TOOL_SCOPES.get(tool_name, WRITE)
    if required not in granted:
        raise HTTPException(status_code=403, detail=f"MCP token lacks required scope: {required!r}")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _service_summary(state: Any) -> dict[str, Any]:
    """Compact {status, phase, task_id} projection of a ServiceState."""
    return {
        "task_id": getattr(state, "task_id", None),
        "status": getattr(state, "status", None),
        "phase": getattr(state, "phase", None),
    }


def _tool_list_workitems() -> dict[str, Any]:
    items = get_store().list()
    rows = [
        {
            "correlation_key": wi.correlation_key,
            "title": wi.title,
            "pfactory": _service_summary(wi.pfactory),
            "aifactory": _service_summary(wi.aifactory),
            "tfactory": _service_summary(wi.tfactory),
            "events": len(wi.timeline),
        }
        for wi in items
    ]
    return {"count": len(rows), "items": rows}


def _tool_get_workitem(correlation_key: str) -> dict[str, Any]:
    wi = get_store().get(correlation_key)
    if wi is None:
        return {"error": f"no work item for {correlation_key!r}"}
    return wi.model_dump(mode="json")


def _tool_get_timeline(correlation_key: str) -> dict[str, Any]:
    summary = summarize_timeline(get_store(), correlation_key)
    if summary is None:
        return {"error": f"no work item for {correlation_key!r}"}
    return summary


def _tool_get_rollups() -> dict[str, Any]:
    return compute_rollups(get_store())


def _tool_get_anomalies() -> dict[str, Any]:
    found = detect_anomalies(get_store())
    return {"count": len(found), "anomalies": found}


# ---------------------------------------------------------------------------
# Board tool handlers — thin adapters over cfactory.card_ops
# ---------------------------------------------------------------------------

# Audit ``endpoint`` for a board mutation that arrived over MCP rather than REST,
# so the trail records which surface an agent used.
_MCP_ENDPOINT = "/mcp"


@dataclass(frozen=True)
class ToolContext:
    """Per-call state a board tool needs: the card store and the audit provenance.

    Resolved once in the endpoint from the request, so the handlers stay pure
    functions of (arguments, context) and never touch the Request.
    """

    cards: CardStore
    audit: card_ops.AuditContext


def _tool_list_cards(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.list_cards(
        ctx.cards,
        status=args.get("status"),
        milestone=args.get("milestone"),
        assignee=args.get("assignee"),
        tier=args.get("tier"),
    )


def _tool_get_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.get_card(ctx.cards, args.get("card_key", "")).model_dump(mode="json")


def _tool_create_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    card = card_ops.create_card(ctx.cards, ctx.audit, CardCreate(**args))
    return card.model_dump(mode="json")


def _tool_update_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Backs update_card, move_card AND reprioritise_card.

    All three are the same operation — a partial update — differing only in which
    field the agent-facing schema exposes. Splitting them in the catalogue makes
    the intent legible to an agent; splitting them here would just be three
    copies of one line. Mirrors REST, where all three are a PATCH.
    """
    fields = {k: v for k, v in args.items() if k != "card_key"}
    card = card_ops.update_card(
        ctx.cards, ctx.audit, args.get("card_key", ""), CardUpdate(**fields)
    )
    return card.model_dump(mode="json")


def _tool_delete_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.delete_card(ctx.cards, ctx.audit, args.get("card_key", ""))


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], ToolContext], Any]] = {
    "cfactory_list_workitems": lambda _a, _c: _tool_list_workitems(),
    "cfactory_get_workitem": lambda args, _c: _tool_get_workitem(args.get("correlation_key", "")),
    "cfactory_get_timeline": lambda args, _c: _tool_get_timeline(args.get("correlation_key", "")),
    "cfactory_get_rollups": lambda _a, _c: _tool_get_rollups(),
    "cfactory_get_anomalies": lambda _a, _c: _tool_get_anomalies(),
    "cfactory_list_cards": _tool_list_cards,
    "cfactory_get_card": _tool_get_card,
    "cfactory_create_card": _tool_create_card,
    "cfactory_update_card": _tool_update_card,
    "cfactory_move_card": _tool_update_card,
    "cfactory_reprioritise_card": _tool_update_card,
    "cfactory_delete_card": _tool_delete_card,
}


def _dispatch_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
    """Run a tool, rendering the domain errors as JSON the agent can read.

    The REST routes turn these same errors into 404/409/422; over JSON-RPC the
    equivalent is an ``{"error": ...}`` payload, which is how the read tools
    already report a missing correlation_key.
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return handler(arguments, ctx)
    except CardNotFoundError as exc:
        return {"error": f"no card {exc.args[0]!r}"}
    except DuplicateCardKeyError as exc:
        return {"error": f"card already exists: {exc.args[0]!r}"}
    except ValidationError as exc:
        return {"error": "invalid arguments", "details": exc.errors(include_url=False)}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 endpoint
# ---------------------------------------------------------------------------


def _result(result: Any, rpc_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(code: int, message: str, rpc_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_context(request: Request) -> ToolContext:
    """Resolve the caller and the stores a board tool operates on.

    Reuses the REST seams as plain functions rather than re-deriving either:
    ``identity_dep`` for the audit actor (the presented key, else "local") and
    ``cards_store_dep`` for the tenant-scoped card store — so an MCP write lands
    in the same partition an ``X-Tenant-Id``-bearing REST write would.
    """
    actor = identity_dep(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        get_keystore(),
    )
    return ToolContext(
        cards=cards_store_dep(request.headers.get("X-Tenant-Id")),
        audit=card_ops.AuditContext(get_audit_store(), actor, endpoint=_MCP_ENDPOINT),
    )


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """JSON-RPC 2.0 MCP endpoint. Handles initialize, tools/list, tools/call."""
    granted = _authenticate(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content=_error(-32700, "Parse error", None))

    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "initialize":
        return JSONResponse(
            _result(
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cfactory", "version": "1.0.0"},
                },
                rpc_id,
            )
        )

    if method == "tools/list":
        return JSONResponse(_result({"tools": MCP_TOOLS}, rpc_id))

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        _require_tool_scope(tool_name, granted)
        try:
            payload = _dispatch_tool(tool_name, arguments, _tool_context(request))
        except Exception:
            logger.exception("[cfactory-mcp] tool call failed tool=%s", tool_name)
            return JSONResponse(_error(-32603, "Internal error", rpc_id))
        return JSONResponse(
            _result(
                {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]},
                rpc_id,
            )
        )

    return JSONResponse(
        status_code=400, content=_error(-32601, f"Method not found: {method}", rpc_id)
    )

"""CFactory MCP server — the single PARR-pipeline visibility surface.

Implements a minimal POST-only JSON-RPC 2.0 MCP transport at ``POST /mcp`` so an
external agent (Claude Code, the ``/parr-run`` conductor) can watch the whole
plan -> code -> test pipeline from one place. CFactory already correlates every
factory's state by ``correlation_key`` (the GitHub issue number); this exposes
that aggregation read-only as MCP tools instead of forcing the agent to poll
three separate factories.

Read-only today — every tool below declares ``READ``. RFC-0019 Phase 2b adds
board WRITE tools, so the scope model lands first (Phase 2a).

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
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import READ, WRITE, extract_key, get_keystore, secret_matches
from .config import get_settings
from .copilot.anomalies import detect_anomalies
from .copilot.tools import rollups as compute_rollups
from .copilot.tools import summarize_timeline
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


# The scope each tool requires. Kept beside the catalog rather than inside the
# tool dicts so ``tools/list`` stays byte-for-byte the MCP shape clients already
# parse. Every tool today is read-only; Phase 2b board tools land here as WRITE.
TOOL_SCOPES: dict[str, str] = {
    "cfactory_list_workitems": READ,
    "cfactory_get_workitem": READ,
    "cfactory_get_timeline": READ,
    "cfactory_get_rollups": READ,
    "cfactory_get_anomalies": READ,
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


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "cfactory_list_workitems": lambda _: _tool_list_workitems(),
    "cfactory_get_workitem": lambda args: _tool_get_workitem(args.get("correlation_key", "")),
    "cfactory_get_timeline": lambda args: _tool_get_timeline(args.get("correlation_key", "")),
    "cfactory_get_rollups": lambda _: _tool_get_rollups(),
    "cfactory_get_anomalies": lambda _: _tool_get_anomalies(),
}


def _dispatch_tool(name: str, arguments: dict) -> Any:
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    return handler(arguments)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 endpoint
# ---------------------------------------------------------------------------


def _result(result: Any, rpc_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(code: int, message: str, rpc_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


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
            payload = _dispatch_tool(tool_name, arguments)
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

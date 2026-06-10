"""CFactory MCP server — the single PARR-pipeline visibility surface.

Implements a minimal POST-only JSON-RPC 2.0 MCP transport at ``POST /mcp`` so an
external agent (Claude Code, the ``/parr-run`` conductor) can watch the whole
plan -> code -> test pipeline from one place. CFactory already correlates every
factory's state by ``correlation_key`` (the GitHub issue number); this exposes
that aggregation read-only as MCP tools instead of forcing the agent to poll
three separate factories.

Read-only by design — no actions, no mutation (CFactory's advise-and-confirm
action surface stays on the REST API behind its own auth).

Enabled when ``CFACTORY_MCP_SECRET`` is set (the bearer token the client sends).
If absent the server accepts all requests (dev convenience); set it in prod.

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

import logging
import os
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _verify_mcp_token(request: Request) -> None:
    expected = os.environ.get("CFACTORY_MCP_SECRET", "")
    if not expected:
        return  # no secret configured — open (dev mode)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid MCP token")


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


def _dispatch_tool(name: str, arguments: dict) -> Any:
    if name == "cfactory_list_workitems":
        return _tool_list_workitems()
    if name == "cfactory_get_workitem":
        return _tool_get_workitem(arguments.get("correlation_key", ""))
    if name == "cfactory_get_timeline":
        return _tool_get_timeline(arguments.get("correlation_key", ""))
    if name == "cfactory_get_rollups":
        return _tool_get_rollups()
    if name == "cfactory_get_anomalies":
        return _tool_get_anomalies()
    return {"error": f"unknown tool: {name}"}


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
    _verify_mcp_token(request)

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
        try:
            payload = _dispatch_tool(tool_name, arguments)
        except Exception as exc:
            logger.exception("[cfactory-mcp] tool call failed tool=%s", tool_name)
            return JSONResponse(_error(-32603, f"Internal error: {exc}", rpc_id))
        import json as _json

        return JSONResponse(
            _result(
                {"content": [{"type": "text", "text": _json.dumps(payload, indent=2)}]},
                rpc_id,
            )
        )

    return JSONResponse(
        status_code=400, content=_error(-32601, f"Method not found: {method}", rpc_id)
    )

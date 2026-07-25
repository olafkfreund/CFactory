"""Capability-discovery manifest — GET /.well-known/agent-skills/index.json (RFC-0019 §3.4).

The entry point an external agent hits *first*: it enumerates what this service
can do before it holds a credential, so this endpoint is deliberately readable
without authentication. That is safe because the payload is public metadata only
— service identity, version, the capability names/descriptions already published
by the MCP tool catalog, and relative paths to the MCP + OpenAPI surfaces. No
tokens, no upstream/internal hostnames, no user or work-item data.

The API-key middleware in :mod:`cfactory.app` guards ``/api/*`` and ``/connect/*``
only, so this path is exempt by construction rather than by an added exception
(see ``tests/test_well_known.py``, which asserts that under an enforced keystore).

The skills list is derived from :data:`cfactory.mcp.MCP_TOOLS` rather than
restated here: CFactory ships no ``.claude/skills`` of its own, and the MCP tool
catalog *is* the set of capabilities it actually exposes to agents. Deriving it
means the manifest cannot drift from the server.

URLs are relative — a client already knows the origin it fetched this from, and
emitting an absolute base would mean publishing a hostname we would then have to
keep honest across local/dev/hosted.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import __version__
from .mcp import MCP_TOOLS

router = APIRouter(tags=["well-known"])

WELL_KNOWN_AGENT_SKILLS_PATH = "/.well-known/agent-skills/index.json"


@router.get(WELL_KNOWN_AGENT_SKILLS_PATH)
async def agent_skills_index() -> dict[str, object]:
    return {
        "service": "cfactory",
        "version": __version__,
        "description": "Agentic control-tower cockpit over the PARR pipeline.",
        # Read-only visibility skills, invoked as MCP tools at the endpoint below.
        "skills": [
            {"name": tool["name"], "description": tool["description"]} for tool in MCP_TOOLS
        ],
        "mcp": {"transport": "http", "endpoint": "/mcp"},
        "openapi": "/openapi.json",
    }

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

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, Response

from . import __version__
from .api_deps import fleet_transport_dep
from .config import get_settings
from .mcp import MCP_TOOLS

logger = logging.getLogger(__name__)

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


# ── Fleet aggregate (RFC-0019 §3.4) ──────────────────────────────────────────
# CFactory is the cockpit host, so it is the one service that can answer "what
# can the whole Factory do?" in a single GET: it folds PFactory's, AIFactory's
# and TFactory's manifests in beside its own. Shape follows the merged contract
# (Factory apis/agent-skills-manifest.schema.json, `kind: "fleet"`): each entry
# is a service manifest body plus the `manifest_url` it was copied from, so the
# aggregate stays a projection and the origin remains the source of truth.
#
# Served alongside the per-service manifest rather than at the same path. The
# contract doc says the cockpit origin answers `kind: fleet` on
# /.well-known/agent-skills/index.json; doing that means replacing CFactory's own
# service manifest, which is a separate (envelope-adding) change — see the PR.

FLEET_AGENT_SKILLS_PATH = "/.well-known/agent-skills/fleet.json"

# Sibling manifests change only on deploy, so re-fetching per request would
# hammer three services for a document that is stable for hours. Matches the
# `max-age=60` the contract puts on the aggregate.
_FLEET_CACHE_TTL_SECONDS = 60.0

# A sibling that is down must not hold the aggregate open — partial availability
# degrades, it does not fail.
_FLEET_FETCH_TIMEOUT_SECONDS = 3.0

_SERVICE_TITLES = {"pfactory": "PFactory", "aifactory": "AIFactory", "tfactory": "TFactory"}

# Copied VERBATIM from the origin manifest — the aggregator must not edit what it
# folds in. An allow-list rather than a passthrough: this endpoint is public, so a
# sibling that puts something unexpected in its manifest cannot leak it onward,
# and the result stays within the schema's `unevaluatedProperties: false`.
_SERVICE_BODY_FIELDS = ("service", "openapi_url", "mcp", "skills", "auth", "generated_at")

# Without these an entry is not a usable service manifest, so treat the origin as
# not-yet-implemented rather than folding in a half-manifest.
_REQUIRED_BODY_FIELDS = ("service", "openapi_url", "mcp", "skills")

# name -> (monotonic timestamp of the fetch, entry). Last-good copies: on a failed
# refresh the stale entry is served with reachable=False, which is exactly what the
# contract asks for. Process-local and unbounded-by-nothing (three keys, by
# construction), so no eviction policy is needed.
_entry_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sibling_base_urls() -> dict[str, str]:
    """The three sibling API origins, from settings — never hardcoded. These are
    operator-configured and runtime-editable (Services view), so a hosted deploy
    publishes its public URLs here and a local one publishes localhost."""
    settings = get_settings()
    return {
        "pfactory": settings.pfactory_api_url,
        "aifactory": settings.aifactory_api_url,
        "tfactory": settings.tfactory_api_url,
    }


def reset_fleet_cache() -> None:
    """Drop the cached sibling manifests. For tests; nothing calls it in prod."""
    _entry_cache.clear()


async def _fetch_entry(
    name: str, base_url: str, transport: httpx.AsyncBaseTransport | None
) -> tuple[dict[str, Any] | None, str]:
    """Fetch one sibling's manifest and fold it into a fleet entry.

    Returns ``(entry, "")`` on success, or ``(None, reason)`` when the origin is
    unreachable or does not serve a usable manifest. The reason is deliberately
    coarse — it is published on a public endpoint, so the exception detail stays
    in the log and never reaches the response.
    """
    manifest_url = base_url.rstrip("/") + WELL_KNOWN_AGENT_SKILLS_PATH
    try:
        async with httpx.AsyncClient(
            timeout=_FLEET_FETCH_TIMEOUT_SECONDS, transport=transport
        ) as client:
            resp = await client.get(manifest_url)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:  # ValueError covers a non-JSON body
        logger.info("[cfactory] fleet manifest fetch failed service=%s: %s", name, exc)
        return None, "unreachable"
    if not isinstance(body, dict) or any(f not in body for f in _REQUIRED_BODY_FIELDS):
        logger.info("[cfactory] fleet manifest incomplete service=%s", name)
        return None, "manifest incomplete"
    entry: dict[str, Any] = {"manifest_url": manifest_url, "fetched_at": _now_iso()}
    entry.update({f: body[f] for f in _SERVICE_BODY_FIELDS if f in body})
    return entry, ""


async def _entry_for(
    name: str, base_url: str, transport: httpx.AsyncBaseTransport | None
) -> tuple[str, dict[str, Any]]:
    """One sibling, tagged with the array it belongs in: ``"service"`` for an
    entry carrying a manifest body, ``"unavailable"`` for one that has none."""
    cached = _entry_cache.get(name)
    if cached is not None and time.monotonic() - cached[0] < _FLEET_CACHE_TTL_SECONDS:
        return "service", cached[1]
    entry, reason = await _fetch_entry(name, base_url, transport)
    if entry is not None:
        _entry_cache[name] = (time.monotonic(), entry)
        return "service", entry
    if cached is not None:
        # The origin is down, not wrong: last-good copy, flagged stale. It still
        # belongs in services[] — it has a full body a consumer can use.
        return "service", {**cached[1], "reachable": False}
    # Never successfully fetched (origin down since startup, or not serving a
    # manifest yet), so there is no body to fold in. Announced rather than
    # omitted, so an agent learns the service exists and is temporarily
    # undiscoverable instead of concluding the fleet is smaller than it is —
    # and announced in `unavailable[]`, which keeps services[] strictly
    # conformant (Factory apis/agent-skills-manifest.schema.json §4.1).
    # Only what configuration knows: no invented version, endpoint or skills.
    return "unavailable", {
        "name": name,
        "title": _SERVICE_TITLES.get(name, name),
        "manifest_url": base_url.rstrip("/") + WELL_KNOWN_AGENT_SKILLS_PATH,
        "reason": reason,
        "checked_at": _now_iso(),
    }


async def _own_entry() -> dict[str, Any]:
    """CFactory's own manifest, folded into the fleet's shape.

    Read from the endpoint above rather than rebuilt, so the two cannot drift.
    The field mapping (``openapi`` → ``openapi_url``, MCP tool → skill
    invocation) exists only because CFactory's service manifest predates the
    merged contract's envelope; it goes away when that endpoint adopts it.
    """
    own = await agent_skills_index()
    own_skills = cast("list[dict[str, str]]", own["skills"])
    skills = [
        {
            # Schema-stable skill ids are kebab-case; the MCP tool name they
            # dispatch to is carried verbatim in the invocation.
            "name": skill["name"].replace("_", "-"),
            "description": skill["description"],
            "invocation": {"kind": "mcp_tool", "tool": skill["name"]},
        }
        for skill in own_skills
    ]
    return {
        "manifest_url": WELL_KNOWN_AGENT_SKILLS_PATH,
        "fetched_at": _now_iso(),
        "service": {
            "name": "cfactory",
            "title": "CFactory",
            "version": __version__,
            "role": "review",
            "description": own["description"],
        },
        "openapi_url": "/openapi.json",
        "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
        "skills": skills,
    }


@router.get(FLEET_AGENT_SKILLS_PATH)
async def agent_skills_fleet(
    response: Response,
    transport: Annotated[httpx.AsyncBaseTransport | None, Depends(fleet_transport_dep)],
) -> dict[str, object]:
    """The whole Factory's capabilities in one unauthenticated GET.

    Best-effort by construction. A sibling that was fetched before and is down
    now keeps its cached body in ``services`` marked ``reachable: false``; one
    that has never been reached is announced in ``unavailable`` instead, so
    every ``services`` entry really does carry a usable manifest.
    """
    bases = _sibling_base_urls()
    siblings = await asyncio.gather(
        *(_entry_for(name, url, transport) for name, url in bases.items())
    )
    with_body = [entry for kind, entry in siblings if kind == "service"]
    unavailable = [entry for kind, entry in siblings if kind == "unavailable"]
    response.headers["Cache-Control"] = "public, max-age=60"
    # A browser-based agent is a first-class consumer of a public manifest, and
    # the app's CORS middleware only allows the cockpit's own dev origin.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return {
        "schema_version": "1",
        "kind": "fleet",
        "fleet": {
            "name": "factory",
            "title": "The Factory",
            "description": (
                "An agent-native software-delivery control plane: PFactory prepares a "
                "governed plan, AIFactory builds it, TFactory verifies it, CFactory "
                "threads the whole run on one correlation key."
            ),
            "aggregator": "cfactory",
        },
        # PARR order: prepare, act, reflect, then the review host that aggregated
        # them. CFactory's own entry is always present, so services[] is never
        # empty even with all three siblings down.
        "services": [*with_body, await _own_entry()],
        "unavailable": unavailable,
        "generated_at": _now_iso(),
    }

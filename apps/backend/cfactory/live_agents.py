"""Live agents — discovery of currently-active AIFactory agent sessions (#33).

The cockpit's "Live agents" panel streams agent terminals from AIFactory's rmux
subsystem. AIFactory exposes a per-task console WebSocket
(``GET /api/tasks/{spec_id}/agent-console/ws``) but *no* "list active sessions"
endpoint, so we derive the active set from the AIFactory task list — the same
``/api/tasks`` the :class:`AIFactoryAdapter` already reads — and gate it on the
upstream rmux capability (``GET /api/capabilities`` → ``{"rmux": bool}``).

Read-only and best-effort: if AIFactory is unreachable or rmux is off, discovery
returns an empty agent list rather than raising. Streaming each session is the
backend WS proxy's job (#34); this module only answers "who is live right now".
"""

from __future__ import annotations

from urllib.parse import quote

from pydantic import BaseModel

from .adapters.aifactory import AIFactoryAdapter
from .adapters.base import AdapterError, BaseHTTPAdapter
from .models import Service
from .status_taxonomy import is_active as _is_active


class LiveAgent(BaseModel):
    """One active agent session surfaced to the cockpit.

    A ``streamable`` agent has a live rmux console (``ws_path`` set — AIFactory).
    A non-streamable row (#184, e.g. a TFactory verify session) is LISTED so the
    panel isn't falsely idle during test-stage work, but has no console to open.
    """

    correlation_key: str
    spec_id: str
    service: Service = Service.AIFACTORY
    title: str | None = None
    phase: str | None = None
    status: str | None = None
    streamable: bool = True
    # Cockpit-side proxy path (the backend re-streams rmux here, #34) — never the
    # raw AIFactory URL, so no upstream token ever reaches the browser. Empty for
    # a non-streamable row.
    ws_path: str = ""


class LiveAgentsResult(BaseModel):
    """Discovery answer: is rmux on upstream, and which agents are live."""

    rmux_enabled: bool
    agents: list[LiveAgent]


def discover_live_agents(adapter: AIFactoryAdapter) -> LiveAgentsResult:
    """List active AIFactory agent sessions via the given adapter.

    Capability-gated: returns ``rmux_enabled=False`` with no agents when the
    upstream console is off. If rmux is on but the task list can't be fetched,
    returns ``rmux_enabled=True`` with no agents (best-effort, never raises).
    """
    if not adapter.rmux_enabled():
        return LiveAgentsResult(rmux_enabled=False, agents=[])
    try:
        items = adapter.list_items()
    except AdapterError:
        return LiveAgentsResult(rmux_enabled=True, agents=[])

    agents = [
        LiveAgent(
            correlation_key=item.correlation_key,
            spec_id=item.task_id,
            title=item.title,
            phase=item.phase,
            status=item.status,
            ws_path=f"/api/live-agents/{quote(item.correlation_key, safe='')}/ws",
        )
        for item in items
        if _is_active(item.status)
    ]
    return LiveAgentsResult(rmux_enabled=True, agents=agents)


def discover_tfactory_agents(adapter: BaseHTTPAdapter) -> list[LiveAgent]:
    """List TFactory's active verify sessions as non-streamable rows (#184).

    Two of the three pipeline stages (plan, test) run real agent work the LIVE
    AGENTS panel was blind to, so it read "no agents running" during an active
    verify. TFactory has no rmux console (terminal streaming stays AIFactory-only),
    so these rows are informational — they surface that verify is busy. Derived
    from the TFactory task list, filtered to a non-terminal (active) status.
    Best-effort; never raises.
    """
    try:
        items = adapter.list_items()
    except AdapterError:
        return []
    return [
        LiveAgent(
            correlation_key=item.correlation_key,
            spec_id=item.task_id,
            service=Service.TFACTORY,
            title=item.title,
            phase=item.phase,
            status=item.status,
            streamable=False,
            ws_path="",
        )
        for item in items
        if _is_active(item.status)
    ]

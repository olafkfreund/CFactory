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
from .adapters.base import AdapterError
from .models import Service
from .status_taxonomy import is_active as _is_active


class LiveAgent(BaseModel):
    """One streamable agent session surfaced to the cockpit."""

    correlation_key: str
    spec_id: str
    service: Service = Service.AIFACTORY
    title: str | None = None
    phase: str | None = None
    status: str | None = None
    # Cockpit-side proxy path (the backend re-streams rmux here, #34) — never the
    # raw AIFactory URL, so no upstream token ever reaches the browser.
    ws_path: str


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

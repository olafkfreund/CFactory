"""Prepared actions: advise + confirm, no autonomous writes.

The cockpit never writes to an upstream service on its own. Each ``propose_*``
tool only BUILDS a :class:`PreparedAction` describing the write it would make;
nothing touches a service until a separate, explicit :func:`execute_action`
call (the confirmation step).

This keeps the human in the loop: propose -> review -> confirm -> execute.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel

from .config import Settings, get_settings
from .models import Service
from .store import WorkItemStore

# Best-effort endpoint contracts for each target service. Kept as module
# constants so they're easy to audit and adjust as the upstream surfaces firm up.
APPROVE_GATE_ENDPOINT = "/api/plans/{session}/approve"
TRIGGER_HANDOFF_ENDPOINT = "/api/tasks/create-and-run"
KICK_HANDBACK_ENDPOINT = "/api/tasks/{task_id}/apply-correction"

ActionKind = Literal["approve_gate", "trigger_handoff", "kick_handback"]


class PreparedAction(BaseModel):
    """A fully-described, not-yet-executed write against an upstream service."""

    kind: ActionKind
    correlation_key: str
    target_service: str  # pfactory | aifactory | tfactory
    method: str  # e.g. "POST"
    endpoint: str  # path on the target service
    payload: dict[str, Any]
    rationale: str  # human-readable "why"


def propose_approve_gate(
    store: WorkItemStore, correlation_key: str
) -> PreparedAction | None:
    """Propose approving PFactory's human-review gate to unlock plan emission."""
    wi = store.get(correlation_key)
    if wi is None:
        return None
    session = wi.pfactory.task_id
    return PreparedAction(
        kind="approve_gate",
        correlation_key=correlation_key,
        target_service=Service.PFACTORY.value,
        method="POST",
        endpoint=APPROVE_GATE_ENDPOINT.format(session=session),
        payload={"approver": "cockpit"},
        rationale=(
            f"Approve the PFactory plan gate for {correlation_key!r} "
            "to unlock emission of the plan downstream."
        ),
    )


def propose_trigger_handoff(
    store: WorkItemStore, correlation_key: str
) -> PreparedAction | None:
    """Propose handing the approved plan off to AIFactory for execution."""
    wi = store.get(correlation_key)
    if wi is None:
        return None
    issue_number = int(correlation_key) if correlation_key.isdigit() else None
    return PreparedAction(
        kind="trigger_handoff",
        correlation_key=correlation_key,
        target_service=Service.AIFACTORY.value,
        method="POST",
        endpoint=TRIGGER_HANDOFF_ENDPOINT,
        payload={
            "correlation_key": correlation_key,
            "issue_number": issue_number,
        },
        rationale=(
            f"Hand the approved plan for {correlation_key!r} to AIFactory "
            "to create and run the coding task."
        ),
    )


def propose_kick_handback(
    store: WorkItemStore, correlation_key: str
) -> PreparedAction | None:
    """Propose re-running AIFactory's QA fixer for a handed-back unit of work."""
    wi = store.get(correlation_key)
    if wi is None:
        return None
    task_id = wi.aifactory.task_id
    return PreparedAction(
        kind="kick_handback",
        correlation_key=correlation_key,
        target_service=Service.AIFACTORY.value,
        method="POST",
        endpoint=KICK_HANDBACK_ENDPOINT.format(task_id=task_id),
        payload={"confirm": True, "source": "cockpit"},
        rationale=(
            f"Re-run the AIFactory QA fixer (apply-correction) for {correlation_key!r} "
            "to address the handback."
        ),
    )


# Registry so callers (and the API) can dispatch by kind.
PROPOSERS = {
    "approve_gate": propose_approve_gate,
    "trigger_handoff": propose_trigger_handoff,
    "kick_handback": propose_kick_handback,
}


def propose(
    store: WorkItemStore, kind: str, correlation_key: str
) -> PreparedAction | None:
    """Dispatch to the matching ``propose_*`` tool.

    Raises ``KeyError`` for an unknown kind; returns ``None`` if no work item.
    """
    proposer = PROPOSERS[kind]
    return proposer(store, correlation_key)


def _base_url_for(settings: Settings, target_service: str) -> str:
    return {
        Service.PFACTORY.value: settings.pfactory_api_url,
        Service.AIFACTORY.value: settings.aifactory_api_url,
        Service.TFACTORY.value: settings.tfactory_api_url,
    }[target_service]


def execute_action(
    action: PreparedAction,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Perform a CONFIRMED action over HTTP. Best-effort: never raises.

    Resolves the target base URL from ``settings`` by ``target_service`` and
    issues ``action.method action.endpoint`` with ``action.payload`` as JSON.
    A custom ``transport`` (e.g. ``httpx.MockTransport``) can be injected for
    hermetic tests, mirroring :class:`BaseHTTPAdapter`.

    Returns ``{"status_code", "ok", "body"}`` on a completed request, or
    ``{"status_code": 0, "ok": False, "error": ...}`` if the request itself
    failed (connection error, timeout, etc).
    """
    settings = settings or get_settings()
    try:
        base_url = _base_url_for(settings, action.target_service)
    except KeyError:
        return {
            "status_code": 0,
            "ok": False,
            "error": f"unknown target_service: {action.target_service!r}",
        }

    try:
        with httpx.Client(base_url=base_url, timeout=5.0, transport=transport) as client:
            resp = client.request(action.method, action.endpoint, json=action.payload)
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return {
            "status_code": resp.status_code,
            "ok": resp.is_success,
            "body": body,
        }
    except httpx.HTTPError as exc:
        return {"status_code": 0, "ok": False, "error": str(exc)}

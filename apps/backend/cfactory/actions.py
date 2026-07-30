"""Prepared actions: advise + confirm, no autonomous writes.

The cockpit never writes to an upstream service on its own. Each ``propose_*``
tool only BUILDS a :class:`PreparedAction` describing the write(s) it would make;
nothing touches a service until a separate, explicit :func:`execute_action`
call (the confirmation step). This keeps the human in the loop:
propose -> review -> confirm -> execute, and every execute is audited.

Endpoints target the factories' shared task API (``/api/tasks/{id}/...``),
verified against the live OpenAPI specs. Writes carry the upstream bearer token
so they pass the factories' ``TokenAuthMiddleware``.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, field_validator

from .config import Settings, get_settings
from .models import Service, WorkItem
from .store import WorkItemStore, _is_terminal

# Only safe HTTP verbs for a confirmed write to an internal service.
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def is_safe_endpoint(endpoint: str) -> bool:
    """True only for a strict root-relative path (no scheme, no host).

    This is the SSRF guard: ``endpoint`` is combined with a fixed per-service
    ``base_url``, but httpx would discard that base_url if given an absolute or
    protocol-relative URL — letting a caller choose an arbitrary host/scheme.
    Constrain it to ``/...`` paths so requests can only ever hit the resolved
    upstream service.
    """
    if not isinstance(endpoint, str) or not endpoint.startswith("/") or endpoint.startswith("//"):
        return False
    parts = urlsplit(endpoint)
    return parts.scheme == "" and parts.netloc == ""


# Code/test task endpoints — AIFactory & TFactory expose this surface and accept
# the task_id we hold for those stages.
APPROVE_PLAN_ENDPOINT = "/api/tasks/{task_id}/approve-plan"
CREATE_PR_ENDPOINT = "/api/tasks/{task_id}/worktree/create-pr"
MERGE_ENDPOINT = "/api/tasks/{task_id}/worktree/merge"
APPLY_CORRECTION_ENDPOINT = "/api/tasks/{task_id}/apply-correction"
RECOVER_ENDPOINT = "/api/tasks/{task_id}/recover"
DELETE_TASK_ENDPOINT = "/api/tasks/{task_id}"

# PFactory plan-stage actions operate on plan *sessions* by bare session_id, NOT
# the generic /api/tasks/{task_id} surface (PFactory only accepts that as the
# compound 'project_id:spec_id', which the cockpit doesn't carry). PFactory has no
# session-delete endpoint, so "remove" is unavailable for a plan-stage item.
PF_SESSION_APPROVE = "/api/plan/sessions/{sid}/approve"
PF_SESSION_REJECT = "/api/plan/sessions/{sid}/reject"
PF_SESSION_PROCESS = "/api/plan/sessions/{sid}/process"

_PFACTORY = Service.PFACTORY.value

ActionKind = Literal[
    "approve_plan",  # approve a plan gate (planning phase)
    "approve_review",  # accept code in human_review → open PR, then merge
    "reject_review",  # send code back to the QA fixer with a reason
    "recover",  # unstick a stalled/errored task
    "delete_task",  # remove the task from the upstream factory
    # RFC-0019 §3.2: send a ready planning card into the factory. Built by
    # cfactory.card_intake, not by a propose_* tool — the "proposal" is the human
    # moving the card to the ready column, so it has no entry in PROPOSERS.
    "dispatch_card",
]


def _check_endpoint(v: str) -> str:
    if not is_safe_endpoint(v):
        raise ValueError("endpoint must be a root-relative path (no scheme or host)")
    return v


class _MethodEndpointModel(BaseModel):
    """Base for models carrying a validated upstream ``method`` + ``endpoint``."""

    @field_validator("endpoint", check_fields=False)
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        return _check_endpoint(v)

    @field_validator("method", check_fields=False)
    @classmethod
    def _validate_method(cls, v: str) -> str:
        m = v.upper()
        if m not in _ALLOWED_METHODS:
            raise ValueError(f"method must be one of {sorted(_ALLOWED_METHODS)}")
        return m


class ActionStep(_MethodEndpointModel):
    """One HTTP write in a (possibly multi-step) prepared action."""

    method: str
    endpoint: str  # root-relative path on the target service (validated)
    payload: dict[str, Any] = {}


class PreparedAction(_MethodEndpointModel):
    """A fully-described, not-yet-executed set of writes against an upstream.

    The primary step is ``method``/``endpoint``/``payload``; ``follow_ups`` are
    run in order only if the primary succeeds (e.g. approve = create-pr → merge).
    """

    kind: ActionKind
    correlation_key: str
    target_service: str  # pfactory | aifactory | tfactory
    method: str
    endpoint: str
    payload: dict[str, Any] = {}
    follow_ups: list[ActionStep] = []
    rationale: str  # human-readable "why", shown in the confirm dialog


def _review_target(wi: WorkItem) -> tuple[str, str] | None:
    """The (service, task_id) of the furthest non-terminal stage holding a task.

    Review / recover / delete act on the stage that is actually in flight — for
    a code task in human_review that's AIFactory; a test in review would be
    TFactory. Terminal (done/failed) stages are skipped. Returns None when no
    actionable stage exists.
    """
    for attr, svc in (
        ("tfactory", Service.TFACTORY),
        ("aifactory", Service.AIFACTORY),
        ("pfactory", Service.PFACTORY),
    ):
        slice_ = getattr(wi, attr)
        if slice_.task_id and not _is_terminal(slice_.status):
            return svc.value, slice_.task_id
    return None


def _delete_target(wi: WorkItem) -> tuple[str, str] | None:
    """The (service, task_id) of the furthest stage holding a task — INCLUDING
    terminal (failed/done) stages.

    Unlike :func:`_review_target` (which only finds an *in-flight* stage for
    approve/reject/recover), Remove must work on a FAILED task: its stages are
    all terminal, so the review target is None and the task would be unremovable.
    A failed task's task_id lives on whichever stage failed, so resolve the
    furthest downstream stage that carries one regardless of status.
    """
    for attr, svc in (
        ("tfactory", Service.TFACTORY),
        ("aifactory", Service.AIFACTORY),
        ("pfactory", Service.PFACTORY),
    ):
        slice_ = getattr(wi, attr)
        if slice_.task_id:
            return svc.value, slice_.task_id
    return None


def propose_approve_plan(store: WorkItemStore, correlation_key: str, note: str | None = None):
    wi = store.get(correlation_key)
    if wi is None or not wi.pfactory.task_id:
        return None
    return PreparedAction(
        kind="approve_plan",
        correlation_key=correlation_key,
        target_service=Service.PFACTORY.value,
        method="POST",
        endpoint=APPROVE_PLAN_ENDPOINT.format(task_id=wi.pfactory.task_id),
        payload={"approver": "cockpit"},
        rationale=f"Approve the plan gate for {correlation_key!r} so it can move downstream.",
    )


def propose_approve_review(store: WorkItemStore, correlation_key: str, note: str | None = None):
    wi = store.get(correlation_key)
    if wi is None:
        return None
    target = _review_target(wi)
    if target is None:
        return None
    service, task_id = target
    if service == _PFACTORY:
        # Plan stage: approve the plan session so it can move downstream.
        return PreparedAction(
            kind="approve_review",
            correlation_key=correlation_key,
            target_service=service,
            method="POST",
            endpoint=PF_SESSION_APPROVE.format(sid=task_id),
            payload={"approver": "cockpit", "feedback": (note or "").strip()},
            rationale=f"Approve the plan {correlation_key!r} so it can move to code.",
        )
    return PreparedAction(
        kind="approve_review",
        correlation_key=correlation_key,
        target_service=service,
        method="POST",
        endpoint=CREATE_PR_ENDPOINT.format(task_id=task_id),
        payload={},
        follow_ups=[ActionStep(method="POST", endpoint=MERGE_ENDPOINT.format(task_id=task_id))],
        rationale=(
            f"Approve {correlation_key!r}: open the pull request for the work, "
            "then merge it. This writes to the repository."
        ),
    )


def propose_reject_review(store: WorkItemStore, correlation_key: str, note: str | None = None):
    wi = store.get(correlation_key)
    if wi is None:
        return None
    target = _review_target(wi)
    if target is None:
        return None
    service, task_id = target
    reason = (note or "").strip() or "Rejected from the cockpit."
    if service == _PFACTORY:
        # Plan stage: reject the plan session (sends it back with feedback).
        return PreparedAction(
            kind="reject_review",
            correlation_key=correlation_key,
            target_service=service,
            method="POST",
            endpoint=PF_SESSION_REJECT.format(sid=task_id),
            payload={"approver": "cockpit", "feedback": reason},
            rationale=f"Reject the plan {correlation_key!r} and send it back: {reason}",
        )
    return PreparedAction(
        kind="reject_review",
        correlation_key=correlation_key,
        target_service=service,
        method="POST",
        endpoint=APPLY_CORRECTION_ENDPOINT.format(task_id=task_id),
        payload={"reason": reason, "source": "cockpit"},
        rationale=f"Reject {correlation_key!r} and send it back to the fixer: {reason}",
    )


def propose_recover(store: WorkItemStore, correlation_key: str, note: str | None = None):
    wi = store.get(correlation_key)
    if wi is None:
        return None
    target = _review_target(wi)
    if target is None:
        return None
    service, task_id = target
    if service == _PFACTORY:
        # Plan stage: re-run the plan session's processing to unstick it.
        return PreparedAction(
            kind="recover",
            correlation_key=correlation_key,
            target_service=service,
            method="POST",
            endpoint=PF_SESSION_PROCESS.format(sid=task_id),
            payload={},
            rationale=f"Re-process the plan session {correlation_key!r} to unstick it.",
        )
    return PreparedAction(
        kind="recover",
        correlation_key=correlation_key,
        target_service=service,
        method="POST",
        endpoint=RECOVER_ENDPOINT.format(task_id=task_id),
        payload={"source": "cockpit"},
        rationale=f"Recover (unstick) the stalled task for {correlation_key!r}.",
    )


def propose_delete_task(store: WorkItemStore, correlation_key: str, note: str | None = None):
    wi = store.get(correlation_key)
    if wi is None:
        return None
    # Remove must work on a FAILED task too, so resolve the target even on a
    # terminal stage (see _delete_target) — _review_target would return None
    # for a failed task and the item would be permanently unremovable.
    target = _delete_target(wi)
    if target is None:
        return None
    service, task_id = target
    if service == _PFACTORY:
        # PFactory plan sessions have no delete endpoint — "remove" is not
        # available for a plan-stage item (use Reject to send it back). The UI
        # disables Remove for plan tasks; this is the backend safety net.
        return None
    return PreparedAction(
        kind="delete_task",
        correlation_key=correlation_key,
        target_service=service,
        method="DELETE",
        endpoint=DELETE_TASK_ENDPOINT.format(task_id=task_id),
        payload={},
        rationale=(
            f"Delete the task for {correlation_key!r} from {service}. "
            "This permanently removes it upstream."
        ),
    )


# Registry so callers (and the API) can dispatch by kind.
PROPOSERS = {
    "approve_plan": propose_approve_plan,
    "approve_review": propose_approve_review,
    "reject_review": propose_reject_review,
    "recover": propose_recover,
    "delete_task": propose_delete_task,
}


def propose(
    store: WorkItemStore, kind: str, correlation_key: str, note: str | None = None
) -> PreparedAction | None:
    """Dispatch to the matching ``propose_*`` tool.

    Raises ``KeyError`` for an unknown kind; returns ``None`` if no actionable
    work item / stage exists for the correlation key.
    """
    return PROPOSERS[kind](store, correlation_key, note)


def _base_url_for(settings: Settings, target_service: str) -> str:
    return {
        Service.PFACTORY.value: settings.pfactory_api_url,
        Service.AIFACTORY.value: settings.aifactory_api_url,
        Service.TFACTORY.value: settings.tfactory_api_url,
    }[target_service]


def _refusal_in_body(body: Any) -> str | None:
    """The failure a response BODY declares, or None if it declares none.

    Factory#460: the status line is not the verdict. The factories answer with a
    ``{"success": bool, "error": str}`` envelope and have historically returned
    refusals inside an HTTP 200 -- GitHub declining a merge came back as
    ``200 {"success": false, "error": "not mergeable"}``. A client that reads
    only ``resp.is_success`` then tells the reviewer "Done.", writes ``ok=true``
    into the audit trail, and runs the follow-up step anyway.

    ``ok`` must mean "the operation succeeded", never "the HTTP call was
    answered". Checked here, at the single seam every action step passes
    through, so no caller can forget it and no future consumer repeats it.
    """
    if not isinstance(body, dict) or body.get("success") is not False:
        return None
    for key in ("error", "detail", "message"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return "upstream reported success=false"


def _do(
    client: httpx.Client, method: str, endpoint: str, payload: dict[str, Any]
) -> dict[str, Any]:
    resp = client.request(method, endpoint, json=payload)
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    refusal = _refusal_in_body(body)
    step: dict[str, Any] = {
        "method": method,
        "endpoint": endpoint,
        "status_code": resp.status_code,
        "ok": resp.is_success and refusal is None,
        "body": body,
    }
    if refusal is not None:
        step["error"] = refusal
    return step


def execute_action(
    action: PreparedAction,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Perform a CONFIRMED action (primary step + any follow-ups) over HTTP.

    Best-effort: never raises. Resolves the target base URL from ``settings`` by
    ``target_service`` and issues each step with the upstream bearer token so it
    passes the factories' auth. Follow-ups run only while the prior step
    succeeds. Returns ``{status_code, ok, body, steps}`` (top-level mirrors the
    last step for backward compatibility), or a ``status_code: 0`` error shape
    if the request itself failed.
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

    steps = [(action.method, action.endpoint, action.payload)] + [
        (s.method, s.endpoint, s.payload) for s in action.follow_ups
    ]
    # Defense in depth: re-check every endpoint reached us as a safe path.
    for _m, ep, _p in steps:
        if not is_safe_endpoint(ep):
            return {
                "status_code": 0,
                "ok": False,
                "error": "unsafe endpoint (must be root-relative)",
            }

    headers = {}
    if settings.upstream_token:
        headers["Authorization"] = f"Bearer {settings.upstream_token}"

    results: list[dict[str, Any]] = []
    try:
        with httpx.Client(
            base_url=base_url, timeout=10.0, transport=transport, headers=headers
        ) as client:
            for method, endpoint, payload in steps:
                res = _do(client, method, endpoint, payload)
                results.append(res)
                if not res["ok"]:
                    break  # stop the chain on first failure (don't merge a PR that didn't open)
    except httpx.HTTPError as exc:
        return {"status_code": 0, "ok": False, "error": str(exc), "steps": results}

    last = results[-1]
    out: dict[str, Any] = {
        "status_code": last["status_code"],
        "ok": all(r["ok"] for r in results),
        "body": last["body"],
        "steps": results,
    }
    # Lift the first step's refusal to the top level: the cockpit renders
    # ``result.error`` and otherwise falls back to the body's ``{detail}``, which
    # a ``{"success": false, "error": ...}`` envelope does not carry. Without
    # this the banner reads "Failed" with no reason at all.
    failed = next((r for r in results if not r["ok"]), None)
    if failed is not None and failed.get("error"):
        out["error"] = failed["error"]
    return out

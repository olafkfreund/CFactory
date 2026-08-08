"""Adapter primitives: the normalized item shape and a small HTTP base.

Each upstream service (PFactory / AIFactory / TFactory) exposes its own REST
surface. Adapters translate those into a common ``AdapterItem`` so the cockpit
can thread work across services by correlation key.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from ..models import Service, ServiceState


class AdapterItem(BaseModel):
    """One unit of work as seen by a single service, normalized."""

    correlation_key: str
    service: Service
    task_id: str
    status: str | None = None
    phase: str | None = None
    title: str | None = None
    repo: str | None = None  # target repo owner/name (W5, Factory #218)
    # #245: PFactory's per-lens plan-review verdict. `gates_passed` alone says
    # THAT a plan is blocked, not WHY, so the cockpit could only render an
    # enabled Approve button and let the click 409 with a lens it never showed.
    # Optional -- absent for every other service and for un-reviewed sessions.
    review: dict[str, Any] | None = None

    def to_state(self) -> ServiceState:
        return ServiceState(
            task_id=self.task_id,
            status=self.status,
            phase=self.phase,
            repo=self.repo,
            # Carried under `extra` so it rides the same slice map the event path
            # writes to, and the frontend reads one shape either way.
            extra={"review": self.review} if self.review else {},
        )


class ServiceProbe(BaseModel):
    """Outcome of an authenticated probe of a service's real data endpoint.

    ``online`` is True only when the data fetch actually succeeds (HTTP 200), so
    a reachable-but-rejecting upstream is never shown as healthy. ``status``
    classifies the failure for the Services view:

    - ``online``       — data endpoint returned 200
    - ``unauthorized`` — 401/403; CFactory's upstream token is missing/wrong
    - ``offline``      — connect/timeout/transport error (process down/unreachable)
    - ``error``        — other HTTP error (4xx/5xx) or an unparseable response
    """

    online: bool
    status: str
    detail: str | None = None


def first(d: dict[str, Any], *keys: str) -> Any | None:
    """Return the first present, non-null value among nested-or-flat keys.

    Supports dotted paths, e.g. first(item, "metadata.githubIssueNumber").
    Tolerates schema drift across the services.
    """
    for key in keys:
        cur: Any = d
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur is not None:
            return cur
    return None


class AdapterError(RuntimeError):
    """Raised when an upstream service is unreachable or returns an error."""


# The one status code that means "the upstream answered, and there is no such
# object". Every other failure means we could not tell — see _get_detail (#249).
_HTTP_NOT_FOUND = 404


class BaseHTTPAdapter:
    """Thin synchronous HTTP client over one service's REST API.

    A custom ``transport`` (e.g. ``httpx.MockTransport``) can be injected for
    hermetic tests.
    """

    service: Service
    list_path: str = "/api/tasks"

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Each factory guards its API with `Authorization: Bearer <APP_API_TOKEN>`.
        # Send it as a default header on every request when configured; unset means
        # local dev against a factory running with auth disabled.
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport, headers=headers
        )

    def _get_json(self, path: str) -> Any:
        try:
            resp = self._client.get(path)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"{self.service.value}: GET {path} failed: {exc}") from exc

    def _get_detail(self, path: str) -> dict[str, Any] | None:
        """GET one object, telling "not there" apart from "could not tell" (#249).

        ``None`` means the upstream *answered* and there is no such object (404),
        or answered with something that is not an object. Every other failure —
        transport error, timeout, 5xx, auth reject — raises :class:`AdapterError`.

        The distinction is the whole point: this is shared by the per-service
        detail wrappers (session/task/test), and the cockpit picks the furthest
        stage that "exists". Returning ``None`` for an unreachable upstream made a
        network blip indistinguishable from a stage that never ran, so the
        task-detail DAG silently downgraded to an earlier stage and latched there.
        Callers that want the old degrade-don't-error behaviour catch
        ``AdapterError`` themselves — and must render the stage as *unknown*, not
        as absent.
        """
        try:
            resp = self._client.get(path)
            if resp.status_code == _HTTP_NOT_FOUND:
                return None
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"{self.service.value}: GET {path} failed: {exc}") from exc
        return data if isinstance(data, dict) else None

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        """Accept either a bare list or {items|tasks|plans|sessions|results|data: [...]}."""
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for key in ("items", "tasks", "plans", "sessions", "results", "data"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [r for r in val if isinstance(r, dict)]
        return []

    def probe(self, *, timeout: float = 4.0) -> ServiceProbe:
        """Authenticated reachability+auth check of the real list endpoint.

        Unlike ``health`` (which counts any HTTP response — even a 401 — as up),
        this distinguishes a genuinely healthy upstream from one that is reachable
        but rejecting CFactory's requests, so the Services view can't show a
        failing data fetch as a green 'online'."""
        try:
            resp = self._client.get(self.list_path, timeout=timeout)
        except httpx.HTTPError as exc:
            return ServiceProbe(online=False, status="offline", detail=str(exc))
        code = resp.status_code
        if code in (401, 403):
            return ServiceProbe(
                online=False,
                status="unauthorized",
                detail=f"{code} {resp.reason_phrase} — check CFACTORY_UPSTREAM_TOKEN",
            )
        if code >= 400:
            return ServiceProbe(online=False, status="error", detail=f"{code} {resp.reason_phrase}")
        return ServiceProbe(online=True, status="online")

    def list_items(self) -> list[AdapterItem]:
        rows = self._rows(self._get_json(self.list_path))
        items = [self._normalize(r) for r in rows]
        return [i for i in items if i is not None]

    def _normalize(self, row: dict[str, Any]) -> AdapterItem | None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        self._client.close()

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

    def to_state(self) -> ServiceState:
        return ServiceState(task_id=self.task_id, status=self.status, phase=self.phase)


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
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def _get_json(self, path: str) -> Any:
        try:
            resp = self._client.get(path)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"{self.service.value}: GET {path} failed: {exc}") from exc

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        """Accept either a bare list or {items|tasks|plans|results: [...]}."""
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for key in ("items", "tasks", "plans", "results", "data"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [r for r in val if isinstance(r, dict)]
        return []

    def list_items(self) -> list[AdapterItem]:
        rows = self._rows(self._get_json(self.list_path))
        items = [self._normalize(r) for r in rows]
        return [i for i in items if i is not None]

    def _normalize(self, row: dict[str, Any]) -> AdapterItem | None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        self._client.close()

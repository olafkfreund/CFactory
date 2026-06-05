"""AIFactory adapter — normalizes execution tasks (code stage)."""

from __future__ import annotations

from typing import Any

from ..models import Service
from .base import AdapterError, AdapterItem, BaseHTTPAdapter, first


class AIFactoryAdapter(BaseHTTPAdapter):
    service = Service.AIFACTORY
    list_path = "/api/tasks"

    def rmux_enabled(self) -> bool:
        """Whether AIFactory's live agent console (rmux) is available.

        Probes the always-mounted ``GET /api/capabilities`` → ``{"rmux": bool}``
        discovery endpoint. Best-effort: any error (service down, old build
        without the endpoint) reads as ``False`` so the cockpit degrades to "live
        agents off" rather than erroring.
        """
        try:
            data = self._get_json("/api/capabilities")
        except AdapterError:
            return False
        return bool(data.get("rmux", False)) if isinstance(data, dict) else False

    def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        """Rich detail for one task: ``GET /api/tasks/{task_id}``.

        Returns the raw task object (status, phase, ``executionProgress``,
        ``subtasks``, …) or ``None`` when the task/service is unavailable —
        best-effort so the cockpit's detail drawer degrades rather than errors.
        """
        try:
            data = self._get_json(f"/api/tasks/{task_id}")
        except AdapterError:
            return None
        return data if isinstance(data, dict) else None

    def _normalize(self, row: dict[str, Any]) -> AdapterItem | None:
        task_id = first(row, "id", "task_id")
        if task_id is None:
            return None
        issue = first(
            row, "metadata.githubIssueNumber", "github_issue", "githubIssueNumber", "issue_number"
        )
        return AdapterItem(
            correlation_key=str(issue if issue is not None else task_id),
            service=self.service,
            task_id=str(task_id),
            status=first(row, "status"),
            phase=first(row, "phase", "current_phase"),
            title=first(row, "title", "name", "description"),
        )

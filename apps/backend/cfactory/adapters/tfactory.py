"""TFactory adapter — normalizes test/verification specs (test stage)."""

from __future__ import annotations

from typing import Any

from ..models import Service
from .base import AdapterError, AdapterItem, BaseHTTPAdapter, first


class TFactoryAdapter(BaseHTTPAdapter):
    service = Service.TFACTORY
    list_path = "/api/tasks"

    def get_test_detail(self, task_id: str) -> dict[str, Any] | None:
        """Rich detail for one test task: ``GET /api/tasks/{task_id}``.

        Returns the raw task object — whose ``subtasks`` carry the ``lane`` tag +
        timing the cockpit aggregates into the test-stage lane pipeline (#94) — or
        ``None`` when unavailable. Best-effort so the drawer degrades, not errors.
        """
        try:
            data = self._get_json(f"/api/tasks/{task_id}")
        except AdapterError:
            return None
        return data if isinstance(data, dict) else None

    def _normalize(self, row: dict[str, Any]) -> AdapterItem | None:
        task_id = first(row, "spec_id", "id", "task_id")
        if task_id is None:
            return None
        # TFactory records its AIFactory provenance in source.json.
        issue = first(
            row,
            "source.aifactory.github_issue",
            "github_issue",
            "githubIssueNumber",
            "issue_number",
            "metadata.githubIssueNumber",
        )
        return AdapterItem(
            correlation_key=str(issue if issue is not None else task_id),
            service=self.service,
            task_id=str(task_id),
            status=first(row, "status"),
            phase=first(row, "phase"),
            title=first(row, "title", "name"),
        )

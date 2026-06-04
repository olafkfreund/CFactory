"""AIFactory adapter — normalizes execution tasks (code stage)."""

from __future__ import annotations

from typing import Any

from ..models import Service
from .base import AdapterItem, BaseHTTPAdapter, first


class AIFactoryAdapter(BaseHTTPAdapter):
    service = Service.AIFACTORY
    list_path = "/api/tasks"

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

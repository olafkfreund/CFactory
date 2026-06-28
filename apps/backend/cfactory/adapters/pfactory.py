"""PFactory adapter — normalizes plan sessions (plan stage)."""

from __future__ import annotations

from typing import Any

from ..models import Service
from .base import AdapterItem, BaseHTTPAdapter, first


class PFactoryAdapter(BaseHTTPAdapter):
    service = Service.PFACTORY
    # PFactory exposes plan sessions at /api/plan/sessions (response {"sessions": [...]}).
    list_path = "/api/plan/sessions"

    def get_session_detail(self, session_id: str) -> dict[str, Any] | None:
        """Rich detail for one plan session: ``GET /api/plan/sessions/{id}``.

        Returns the raw session object — which carries the decomposed ``epic``
        (with ``children`` + their ``depends_on`` edges) used to draw the
        plan-stage execution diagram (#94) — or ``None`` when unavailable.
        Best-effort so the cockpit's detail drawer degrades rather than errors.
        """
        return self._get_detail(f"/api/plan/sessions/{session_id}")

    def _normalize(self, row: dict[str, Any]) -> AdapterItem | None:
        task_id = first(row, "session_id", "id")
        if task_id is None:
            return None
        issue = first(
            row, "github_issue", "githubIssueNumber", "issue_number", "metadata.githubIssueNumber"
        )
        return AdapterItem(
            correlation_key=str(issue if issue is not None else task_id),
            service=self.service,
            task_id=str(task_id),
            status=first(row, "board_state", "status"),
            phase="plan",
            title=first(row, "title", "name"),
            repo=first(row, "repo"),  # W5 (#218): target repo owner/name
        )

"""AIFactory adapter — normalizes execution tasks (code stage)."""

from __future__ import annotations

from typing import Any

from cfactory.models import Service

from .base import AdapterError, AdapterItem, BaseHTTPAdapter, created_at_of, first


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
        return self._get_detail(f"/api/tasks/{task_id}")

    def _normalize(self, row: dict[str, Any]) -> AdapterItem | None:
        task_id = first(row, "id", "task_id")
        if task_id is None:
            return None
        reason = first(row, "reviewReason", "review_reason")
        question = None
        if reason in ("needs_input", "approval_rejected"):
            try:
                questions = self._get_json(f"/api/tasks/{task_id}/questions")
                if isinstance(questions, list):
                    pending = next(
                        (
                            q
                            for q in questions
                            if isinstance(q, dict)
                            and q.get("status") in ("open", "expired", "rejected")
                        ),
                        None,
                    )
                    if pending:
                        question = {
                            key: pending[key]
                            for key in (
                                "qid",
                                "question",
                                "choices",
                                "needs_approval",
                                "status",
                                "asked_at",
                                "timeout_minutes",
                            )
                            if key in pending
                        }
            except AdapterError:
                pass  # The pause reason still survives an unavailable question endpoint.
        slug = first(row, "project_slug", "projectSlug", "project.slug")
        return AdapterItem(
            correlation_key=str(first(row, "correlation_key", "correlationKey") or task_id),
            service=self.service,
            task_id=str(task_id),
            status=first(row, "status"),
            phase=first(row, "phase", "current_phase"),
            title=first(row, "title", "name", "description"),
            repo=first(row, "repo"),  # W5 (#218): target repo owner/name
            created_at=created_at_of(row),
            review_reason=reason if isinstance(reason, str) else None,
            question=question,
            project_slug=slug if isinstance(slug, str) else None,
        )

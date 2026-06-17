"""TFactory adapter — normalizes test/verification specs (test stage)."""

from __future__ import annotations

import httpx
from typing import Any

from ..models import Service
from .base import AdapterError, AdapterItem, BaseHTTPAdapter, first

# The canonical TFactory task/evidence API is mounted under this prefix.
_TF_PREFIX = "/api/tfactory/tasks"


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

    def get_evidence_manifest(self, spec_id: str) -> dict[str, list[str]]:
        """Browser-lane media captured for a spec: screenshot + recording file
        names, from ``GET /api/tfactory/tasks/{spec}`` artefacts. Empty lists when
        none/unavailable (best-effort — the cockpit degrades, never errors)."""
        try:
            data = self._get_json(f"{_TF_PREFIX}/{spec_id}")
        except AdapterError:
            return {"screenshots": [], "videos": []}
        arts = (data or {}).get("artefacts", {}) if isinstance(data, dict) else {}
        return {
            "screenshots": list((arts.get("screenshots") or {}).get("files") or []),
            "videos": list((arts.get("videos") or {}).get("files") or []),
        }

    def fetch_media(
        self, spec_id: str, kind: str, name: str
    ) -> tuple[bytes, str] | None:
        """Fetch one screenshot/recording's raw bytes + content-type so CFactory
        can proxy it same-origin (the browser is authenticated to CFactory, not
        TFactory). ``kind`` is ``screenshots`` or ``videos``. None when missing."""
        if kind not in ("screenshots", "videos"):
            return None
        try:
            resp = self._client.get(f"{_TF_PREFIX}/{spec_id}/{kind}/{name}")
            resp.raise_for_status()
        except httpx.HTTPError:
            return None
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

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

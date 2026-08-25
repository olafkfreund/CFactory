"""TFactory adapter — normalizes test/verification specs (test stage)."""

from __future__ import annotations

from typing import Any

import httpx

from cfactory.models import Service

from .base import AdapterError, AdapterItem, BaseHTTPAdapter, first

# The canonical TFactory task/evidence API is mounted under this prefix.
_TF_PREFIX = "/api/tfactory/tasks"


class TFactoryAdapter(BaseHTTPAdapter):
    service = Service.TFACTORY
    # The TEST stage lives in the spec-ingest task store, exposed under
    # /api/tfactory/tasks ({"tasks": [...]}). The generic /api/tasks is the
    # agent-task store and is empty for verification runs — polling it left the
    # cockpit's TEST column permanently empty even for triaged tasks.
    list_path = "/api/tfactory/tasks"

    def get_test_detail(self, task_id: str) -> dict[str, Any] | None:
        """Rich detail for one test task, shaped for the lane-pipeline diagram (#94).

        The lane-tagged subtasks live in the spec's **test plan**, under
        ``phases[].subtasks[]``, and the only route that serves it is
        ``GET /api/tfactory/tasks/{spec}/test-plan.json``.

        This used to call ``GET /api/tasks/{task_id}`` — the generic *agent-task*
        store, the same one ``list_path`` was already moved off because it is empty
        for verification runs. That endpoint requires a ``project_id:spec_id`` key
        and rejects anything else with a 400, while ``_normalize`` hands it a bare
        spec id. So it answered 400 for every task ever asked about, the subtask
        list came back empty, and the test graph never rendered for any work item
        at any status — including fully triaged ones (#260).

        Returns a dict in the shape ``_normalize_test`` expects, or ``None`` when
        the spec has no plan (a 404 — e.g. ``planner_failed``, where no plan was
        ever written).
        """
        plan = self._get_detail(f"{_TF_PREFIX}/{task_id}/test-plan.json")
        if plan is None:
            return None
        phases = plan.get("phases")
        # Deliberately NOT `phases[].chunks`: TFactory emits it as a back-compat
        # duplicate of `subtasks` with identical contents, so reading both would
        # double every lane's member count and its "(n/m) done" label.
        subtasks = [
            s
            for p in (phases if isinstance(phases, list) else [])
            if isinstance(p, dict) and isinstance(p.get("subtasks"), list)
            for s in p["subtasks"]
            if isinstance(s, dict)
        ]
        return {
            "id": task_id,
            "spec_id": task_id,
            "title": plan.get("feature"),
            "status": plan.get("status"),
            "updated_at": plan.get("updated_at"),
            "subtasks": subtasks,
            "lane_progress": self._lane_progress(task_id),
        }

    def _lane_progress(self, task_id: str) -> dict[str, Any] | None:
        """Per-lane EXECUTION state from the run's status.json (#431).

        The test plan above describes what was *generated*; nothing in it says
        whether a lane ever ran. A subtask whose generation completed reports
        ``completed`` either way, which is how a spec with zero executed lanes
        came to render as "Browser (8/8) STAGE COMPLETE".

        Best-effort on purpose: ``None`` when the status is unreadable or the
        field is absent, and the caller then keeps its previous behaviour rather
        than reporting that nothing ran. A TFactory that predates the field
        would otherwise repaint every healthy lane as pending — the same bug
        pointing the other way.
        """
        try:
            data = self._get_json(f"{_TF_PREFIX}/{task_id}")
        except AdapterError:
            return None
        status_json = (data or {}).get("status_json") if isinstance(data, dict) else None
        progress = (status_json or {}).get("lane_progress")
        return progress if isinstance(progress, dict) and progress else None

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

    def fetch_media(self, spec_id: str, kind: str, name: str) -> tuple[bytes, str] | None:
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
            repo=first(row, "repo"),  # W5 (#218): target repo owner/name
        )

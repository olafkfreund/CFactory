"""Shared harness for the planning-card backend tests.

Both the RFC-0019 Phase 3 intake tests and the RFC-0020 Phase 7 stage-action
tests need the same two things: a mock transport standing in for the factories'
intake doors, and an app wired to temp card / work-item / audit stores. They are
here rather than in each file because the stage tests need a strict SUPERSET of
what the intake tests need (three doors instead of two, and per-path failures),
and two divergent copies of an intake mock is exactly how the two suites would
start disagreeing about what a dispatch looks like.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from cfactory.app import audit_dep, cards_store_dep, create_app, store_dep
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from cfactory.store import WorkItemStore
from fastapi.testclient import TestClient

from cfactory.api_deps import action_transport_dep  # isort: skip


class Upstream:
    """Records every intake POST and answers with a canned response.

    One instance stands in for ALL THREE factories, which is what a stage
    sequence needs: plan lands on PFactory, code on AIFactory, test on TFactory,
    and the test has to see them happen in that order. ``fail_at`` makes a single
    door fail so a mid-sequence failure can be asserted without breaking the
    stages either side of it.
    """

    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self.body = {"task_id": "task-7"} if body is None else body
        self.overrides: dict[str, tuple[int, dict]] = {}
        self.calls: list[tuple[str, dict]] = []

    @property
    def paths(self) -> list[str]:
        """Just the paths hit, in order — what a sequence assertion reads."""
        return [path for path, _ in self.calls]

    def fail_at(self, path: str, status_code: int = 500) -> None:
        self.overrides[path] = (status_code, {"error": "boom"})

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.calls.append((path, json.loads(request.content or b"{}")))
            code, body = self.overrides.get(path, (self.status_code, self.body))
            return httpx.Response(code, json=body)

        return httpx.MockTransport(handler)

    def payload_for(self, path: str) -> dict[str, Any] | None:
        """The body sent to ``path``, latest call wins. None if never called."""
        for called, payload in reversed(self.calls):
            if called == path:
                return payload
        return None


def build_client(
    cards: CardStore, items: WorkItemStore, audit: AuditStore, upstream: Upstream
) -> TestClient:
    """An app whose card / work-item / audit stores and outbound transport are
    all the temp ones, so a test sees every dispatch and every audit entry."""
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: cards
    app.dependency_overrides[store_dep] = lambda: items
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = upstream.transport
    return TestClient(app)

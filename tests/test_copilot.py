"""Tests for the agentic copilot scaffold (#13) — hermetic via a fake runner."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from cfactory.app import copilot_dep, create_app, store_dep
from cfactory.copilot import Copilot, build_board_snapshot
from cfactory.models import CompletionEvent, Service


def _seed(store, key="42", service=Service.TFACTORY, status="triaged"):
    store.upsert_from_event(CompletionEvent(
        correlation_key=key, service=service, task_id="t1", status=status,
        phase=service.value, updated_at=datetime(2026, 6, 4, tzinfo=timezone.utc)))


def test_snapshot_includes_keys_and_statuses(store):
    _seed(store, "42", Service.PFACTORY, "human_review")
    _seed(store, "42", Service.TFACTORY, "triaged")
    snap = build_board_snapshot(store)
    assert "#42" in snap
    assert "human_review" in snap and "triaged" in snap


def test_ask_passes_snapshot_to_runner_and_returns_answer(store):
    _seed(store, "42", Service.AIFACTORY, "coding")
    captured: dict = {}

    def fake_runner(question, context, system_prompt):
        captured["question"] = question
        captured["context"] = context
        captured["system_prompt"] = system_prompt
        return "Feature #42 is in the code stage (coding)."

    copilot = Copilot(store, runner=fake_runner)
    result = copilot.ask("where is #42?")

    assert result.answer.startswith("Feature #42")
    assert result.work_items_considered == 1
    assert "#42" in captured["context"]          # board snapshot reached the LLM
    assert captured["question"] == "where is #42?"
    assert "CFactory pipeline copilot" in captured["system_prompt"]


def test_copilot_ask_endpoint(store):
    _seed(store, "7", Service.PFACTORY, "human_review")
    copilot = Copilot(store, runner=lambda q, c, s: f"answer[{len(c)}]")

    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    app.dependency_overrides[copilot_dep] = lambda: copilot
    client = TestClient(app)

    resp = client.post("/api/copilot/ask", json={"question": "status of #7?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"].startswith("answer[")
    assert body["work_items_considered"] == 1

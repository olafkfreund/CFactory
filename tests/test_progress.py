"""Tests for live progress (#v2 P3) — pure mappers, hub, poll-once, endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from cfactory.adapters import AIFactoryAdapter, AdapterItem
from cfactory.app import create_app, progress_hub_dep
from cfactory.models import Service
from cfactory.progress import (
    LiveProgress,
    LiveProgressHub,
    poll_progress_once,
    progress_from_aifactory_frame,
    progress_from_item,
)


def test_progress_from_item_is_coarse():
    item = AdapterItem(correlation_key="42", service=Service.AIFACTORY, task_id="t", status="coding", phase="code")
    lp = progress_from_item(item)
    assert lp.correlation_key == "42" and lp.service is Service.AIFACTORY
    assert lp.phase == "code" and lp.percent is None  # coarse: no %


def test_progress_from_aifactory_frame_has_percent():
    lp = progress_from_aifactory_frame({"phase": "coding", "percentage": 40.0, "subtask": "auth"}, "42")
    assert lp.percent == 40.0 and lp.phase == "coding" and lp.subtask == "auth"


def test_hub_keeps_latest_per_key():
    hub = LiveProgressHub()
    hub.update(LiveProgress(correlation_key="1", service=Service.PFACTORY, phase="plan", updated_at=datetime(2026, 6, 5, 10, tzinfo=timezone.utc)))
    hub.update(LiveProgress(correlation_key="1", service=Service.AIFACTORY, phase="code", updated_at=datetime(2026, 6, 5, 11, tzinfo=timezone.utc)))
    snap = hub.snapshot()
    assert len(snap) == 1 and snap[0].phase == "code"


def test_poll_progress_once_folds_into_hub():
    ai = AIFactoryAdapter("http://x", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"tasks": [
            {"id": "t1", "status": "coding", "phase": "code", "metadata": {"githubIssueNumber": 7}}]})))
    hub = LiveProgressHub()
    changed = poll_progress_once(hub, [ai])
    assert len(changed) == 1
    snap = hub.snapshot()
    assert snap[0].correlation_key == "7" and snap[0].phase == "code"


def test_progress_endpoint(monkeypatch):
    hub = LiveProgressHub()
    hub.update(LiveProgress(correlation_key="9", service=Service.TFACTORY, phase="test", percent=80.0, updated_at=datetime(2026, 6, 5, 12, tzinfo=timezone.utc)))
    app = create_app()
    app.dependency_overrides[progress_hub_dep] = lambda: hub
    client = TestClient(app)
    body = client.get("/api/progress").json()
    assert body["count"] == 1
    assert body["items"][0]["correlation_key"] == "9" and body["items"][0]["percent"] == 80.0

"""Tests for the upstream WebSocket subscriber (#10)."""

from __future__ import annotations

import json

import pytest
from cfactory.config import Settings
from cfactory.models import Service
from cfactory.upstream_ws import handle_message, parse_upstream_message
from cfactory.ws import ConnectionManager


def test_parse_valid_message():
    raw = json.dumps(
        {
            "correlation_key": "55",
            "task_id": "t1",
            "status": "coding",
            "phase": "code",
            "updated_at": "2026-06-04T12:00:00+00:00",
        }
    )
    ev = parse_upstream_message(Service.AIFACTORY, raw)
    assert ev is not None
    assert ev.correlation_key == "55"
    assert ev.service is Service.AIFACTORY
    assert ev.status == "coding"


def test_parse_extracts_issue_and_defaults_timestamp():
    raw = json.dumps({"metadata": {"githubIssueNumber": 7}, "id": "t9", "board_state": "done"})
    ev = parse_upstream_message(Service.PFACTORY, raw)
    assert ev is not None
    assert ev.correlation_key == "7"
    assert ev.status == "done"
    assert ev.updated_at is not None  # defaulted


def test_parse_rejects_garbage():
    assert parse_upstream_message(Service.TFACTORY, "not json") is None
    assert parse_upstream_message(Service.TFACTORY, json.dumps([1, 2])) is None
    assert parse_upstream_message(Service.TFACTORY, json.dumps({"only": "noise"})) is None


@pytest.mark.asyncio
async def test_handle_message_persists_and_returns_event(store):
    raw = json.dumps(
        {
            "correlation_key": "55",
            "task_id": "t1",
            "status": "coding",
            "updated_at": "2026-06-04T12:00:00+00:00",
        }
    )
    ev = await handle_message(Service.AIFACTORY, raw, store, ConnectionManager())

    assert ev is not None and ev.correlation_key == "55"
    wi = store.get("55")
    assert wi is not None
    assert wi.aifactory.status == "coding"


def test_upstream_ws_urls_target_ws_events_not_api_ws():
    """The upstream live feed is `/ws/events` on every service — NOT `/api/ws`.

    `/api/ws` is CFactory's OWN cockpit endpoint (routes_ws); PFactory/AIFactory/
    TFactory serve their live feed at `/ws/events` (server/websockets/events.py).
    Dialing `/api/ws` upstream hits no route → Starlette 403s the WS, the cockpit
    gets zero pushed events, and Mission Control looks dead while polling-backed
    task lists still populate. Regression guard for that outage.
    """
    s = Settings(
        pfactory_api_url="http://pf:3105",
        aifactory_api_url="https://ai:3101",
        tfactory_api_url="http://tf:3103/",
    )
    urls = s.upstream_ws_urls()
    assert urls["pfactory"] == "ws://pf:3105/ws/events"
    assert urls["aifactory"] == "wss://ai:3101/ws/events"
    assert urls["tfactory"] == "ws://tf:3103/ws/events"  # trailing slash trimmed
    assert all(u.endswith("/ws/events") for u in urls.values())
    assert not any("/api/ws" in u for u in urls.values())

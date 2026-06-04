"""Tests for the upstream WebSocket subscriber (#10)."""

from __future__ import annotations

import json

import pytest
import websockets

from cfactory.models import Service
from cfactory.upstream_ws import consume_once, parse_upstream_message
from cfactory.ws import ConnectionManager


def test_parse_valid_message():
    raw = json.dumps({
        "correlation_key": "55", "task_id": "t1", "status": "coding",
        "phase": "code", "updated_at": "2026-06-04T12:00:00+00:00",
    })
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
async def test_consume_once_ingests_from_live_socket(store):
    async def handler(websocket):
        await websocket.send(json.dumps({
            "correlation_key": "55", "task_id": "t1", "status": "coding",
            "updated_at": "2026-06-04T12:00:00+00:00",
        }))
        # Stay open briefly so the client can read before close.
        try:
            await websocket.recv()
        except Exception:  # noqa: BLE001
            pass

    server = await websockets.serve(handler, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ev = await consume_once(f"ws://localhost:{port}", Service.AIFACTORY, store, ConnectionManager())
    finally:
        server.close()
        await server.wait_closed()

    assert ev is not None and ev.correlation_key == "55"
    wi = store.get("55")
    assert wi is not None
    assert wi.aifactory.status == "coding"

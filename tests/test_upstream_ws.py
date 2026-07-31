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


def test_parse_carries_the_plan_review_block():
    """PFactory reaches the cockpit over this socket, so the verdict must too (#245)."""
    review = {
        "gates_passed": False,
        "threshold": 0.75,
        "aggregate_score": 0.94,
        "lenses": [
            {"lens": "security", "score": 0.70, "findings": [{"title": "No auth criteria"}]}
        ],
    }
    raw = json.dumps(
        {
            "correlation_key": "27",
            "task_id": "027-money-safe-vat-quote-endpoint",
            "status": "human_review",
            "review": review,
        }
    )
    ev = parse_upstream_message(Service.PFACTORY, raw)
    assert ev is not None
    assert ev.review == review
    assert ev.review["gates_passed"] is False


def test_parse_tolerates_a_missing_or_malformed_review():
    """Today's messages carry no review; they must parse exactly as before."""
    base = {"correlation_key": "27", "task_id": "t", "status": "human_review"}

    absent = parse_upstream_message(Service.PFACTORY, json.dumps(base))
    assert absent is not None and absent.review is None

    # A non-dict `review` must not blow up ingestion or reach the model as junk.
    malformed = parse_upstream_message(
        Service.PFACTORY, json.dumps({**base, "review": "gates_passed"})
    )
    assert malformed is not None and malformed.review is None


def test_parses_pfactorys_actual_envelope():
    """PFactory wraps everything as {"type": ..., "payload": {...}} (#285).

    This is the shape `broadcast_event` in PFactory's websockets/events.py puts
    on the wire — nested, and camelCase. The parser only looked for flat
    snake_case keys, so every one of these was discarded and the subscriber
    contributed nothing. Nobody noticed because the polling adapter carries
    PFactory state independently.
    """
    raw = json.dumps(
        {
            "type": "task:status",
            "payload": {
                "taskId": "027-money-safe-vat-quote-endpoint",
                "status": "human_review",
                "correlation_key": "27",
            },
        }
    )
    ev = parse_upstream_message(Service.PFACTORY, raw)
    assert ev is not None, "PFactory's own envelope must parse"
    assert ev.task_id == "027-money-safe-vat-quote-endpoint"
    assert ev.status == "human_review"
    assert ev.correlation_key == "27"


def test_envelope_review_block_also_survives():
    raw = json.dumps(
        {
            "type": "task:status",
            "payload": {
                "taskId": "t",
                "status": "human_review",
                "review": {"gates_passed": False, "threshold": 0.75},
            },
        }
    )
    ev = parse_upstream_message(Service.PFACTORY, raw)
    assert ev is not None and ev.review["gates_passed"] is False


def test_flat_messages_still_win_over_the_envelope():
    """The services that already worked must be unaffected."""
    raw = json.dumps(
        {
            "correlation_key": "55",
            "task_id": "flat",
            "status": "coding",
            "payload": {"taskId": "nested", "status": "ignored"},
        }
    )
    ev = parse_upstream_message(Service.AIFACTORY, raw)
    assert ev is not None
    assert ev.task_id == "flat" and ev.status == "coding"


def test_garbage_is_still_rejected():
    """Accepting a second envelope must not turn the parser into a yes-man."""
    assert parse_upstream_message(Service.PFACTORY, json.dumps({"type": "ping"})) is None
    assert parse_upstream_message(Service.PFACTORY, json.dumps({"payload": {}})) is None
    assert parse_upstream_message(Service.PFACTORY, "not json") is None

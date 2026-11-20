"""Investigation cards cannot enter ordinary card sync or factory intake."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cards_harness import Upstream, build_client
from cfactory import card_intake, card_ops, config, github_sync, issue_import, mcp
from cfactory.audit import AuditStore
from cfactory.card_intake import InvestigationCardError
from cfactory.card_ops import AuditContext
from cfactory.cards import CardRow, CardStore, CardUpdate
from cfactory.config import Settings
from cfactory.github_sync import IssueRef
from cfactory.models import ServiceState, Stage, WorkItem
from cfactory.store import WorkItemStore
from runners.github.providers.protocol import IssueData
from sqlalchemy import select

_TEST_HMAC = "guard-test-hmac"
_TEST_TOKEN = "fake-github-token"  # noqa: S105 — fake token, no credential


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    cards = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    items = WorkItemStore(f"sqlite:///{tmp_path / 'items.db'}")
    audit = AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)
    upstream = Upstream()
    settings = Settings(
        intake_project_id="proj-1",
        github_token=_TEST_TOKEN,
        github_repo="acme/widgets",
        github_api_url="https://gh.test",
    )
    monkeypatch.setattr(card_intake, "get_settings", lambda: settings)
    monkeypatch.setattr(github_sync, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", upstream.transport)
    monkeypatch.setenv("CFACTORY_MCP_SECRET", "guard-test-secret")
    config.reset_settings()
    card = cards.create_investigation_card(
        title="Investigate a failure",
        description="Find the cause",
        repository_id=1,
        binding={"repository_id": 1, "repo": "acme/widgets", "aifactory_project_id": "proj-1"},
    )
    # Make ordinary hooks eligible. Without their guards these values reach the
    # provider or intake, rather than returning early on status/tier/key checks.
    with cards._session.begin() as session:
        row = session.scalars(select(CardRow).where(CardRow.card_key == card.card_key)).one()
        assert row is not None
        row.status = "ready"
        row.tier = "low"
        row.correlation_key = "joined-1"
        row.repository_id = None
    card = cards.get(card.card_key)
    assert card is not None
    assert github_sync.sync_enabled(cards.git_target_for_card(card, settings))
    assert card_intake.aifactory_project_id(cards, settings, card) == "proj-1"
    client = build_client(cards, items, audit, upstream)
    yield cards, audit, upstream, client, card
    config.reset_settings()


def _unchanged(cards, card):
    current = cards.get(card.card_key)
    assert current is not None
    assert (current.status, current.tier, current.stage_runs) == (
        card.status,
        card.tier,
        card.stage_runs,
    )


def test_update_guard_blocks_write_and_intake(guarded):
    cards, audit, upstream, _, card = guarded
    assert card is not None
    with pytest.raises(InvestigationCardError):
        card_ops.update_card(
            cards, AuditContext(audit, "tester"), card.card_key, CardUpdate(status="done")
        )
    _unchanged(cards, card)
    assert upstream.calls == []


def test_sync_card_guard_blocks_provider(guarded):
    cards, _, upstream, _, card = guarded
    assert card is not None
    assert github_sync.sync_card(cards, card, transport=upstream.transport()) == {
        "ok": False,
        "skipped": "investigation_card",
    }
    _unchanged(cards, card)
    assert upstream.calls == []


def test_maybe_sync_guard_blocks_provider(guarded):
    cards, _, upstream, _, card = guarded
    assert card is not None
    assert github_sync.maybe_sync(cards, card, transport=upstream.transport()) is None
    _unchanged(cards, card)
    assert upstream.calls == []


def test_explicit_sync_guard_blocks_provider(guarded):
    cards, audit, upstream, _, card = guarded
    assert card is not None
    with pytest.raises(InvestigationCardError):
        card_ops.sync_card_github(cards, AuditContext(audit, "tester"), card.card_key)
    _unchanged(cards, card)
    assert upstream.calls == []


def test_maybe_dispatch_guard_blocks_intake(guarded):
    cards, _, upstream, _, card = guarded
    assert card is not None
    assert card_intake.maybe_dispatch(cards, card, transport=upstream.transport()) is None
    _unchanged(cards, card)
    assert upstream.calls == []


def test_dispatch_card_guard_blocks_intake(guarded):
    cards, _, upstream, _, card = guarded
    assert card is not None
    result = card_intake.dispatch_card(cards, card, transport=upstream.transport())
    assert result == {"dispatched": False, "ok": False, "reason": "investigation_card"}
    _unchanged(cards, card)
    assert upstream.calls == []


@pytest.mark.parametrize("stage", [Stage.PLAN, Stage.CODE, Stage.TEST])
def test_dispatch_stage_guard_blocks_intake(guarded, stage):
    cards, _, upstream, _, card = guarded
    assert card is not None
    if stage is Stage.TEST:
        with cards._session.begin() as session:
            row = session.scalars(select(CardRow).where(CardRow.card_key == card.card_key)).one()
            row.stage_runs = {
                **row.stage_runs,
                "code": {"service": "aifactory", "status": "done", "detail": None},
            }
        card = cards.get(card.card_key)
        assert card is not None
    with pytest.raises(InvestigationCardError):
        card_intake.dispatch_stage(cards, card, stage, transport=upstream.transport())
    _unchanged(cards, card)
    assert upstream.calls == []


def test_start_sequence_guard_blocks_intake(guarded):
    cards, _, upstream, _, card = guarded
    assert card is not None
    with pytest.raises(InvestigationCardError):
        card_intake.start_sequence(cards, card, transport=upstream.transport())
    _unchanged(cards, card)
    assert upstream.calls == []


def test_apply_status_ignores_joined_investigation(guarded):
    cards, _, upstream, _, card = guarded
    assert card is not None
    item = WorkItem(
        correlation_key="joined-1", aifactory=ServiceState(task_id="t", status="completed")
    )
    assert card_intake.apply_status(cards, item, transport=upstream.transport()) is None
    _unchanged(cards, card)
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/cards/{key}", {"status": "done"}),
        ("post", "/api/cards/{key}/sync-github", None),
        *[
            ("post", f"/api/cards/{{key}}/actions/{stage}", None)
            for stage in ("plan", "code", "test", "run")
        ],
    ],
)
def test_rest_guards_return_409(guarded, method, path, body):
    cards, _, upstream, client, card = guarded
    assert card is not None
    response = getattr(client, method)(path.format(key=card.card_key), json=body)
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "investigation_card"}}
    _unchanged(cards, card)
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("cfactory_update_card", {"status": "done"}),
        ("cfactory_move_card", {"status": "done"}),
        ("cfactory_reprioritise_card", {"priority": 1}),
        ("cfactory_sync_card_github", {}),
        *[(f"cfactory_{stage}_card", {}) for stage in ("plan", "code", "test", "run")],
    ],
)
def test_mcp_guards_return_tool_error(guarded, tool, arguments):
    cards, _, upstream, client, card = guarded
    assert card is not None
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer guard-test-secret"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"card_key": card.card_key, **arguments}},
        },
    )
    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["reason"] == "investigation_card"
    assert "error" in payload
    _unchanged(cards, card)
    assert upstream.calls == []


def test_issue_import_skips_investigation_with_matching_issue(guarded):
    cards, _, upstream, _, card = guarded
    assert card is not None
    with cards._session.begin() as session:
        row = session.scalars(select(CardRow).where(CardRow.card_key == card.card_key)).one()
        assert row is not None
        row.issue_ref = "acme/widgets#42"
    before = cards.get(card.card_key)
    now = datetime.now(UTC)
    issue = IssueData(
        number=42,
        title="Changed",
        body="",
        state="closed",
        labels=[],
        author="tester",
        created_at=now,
        updated_at=now,
        url="https://gh.test/acme/widgets/issues/42",
    )
    assert issue_import._import_one(cards, IssueRef("acme/widgets", "42"), issue) == "skipped"
    _unchanged(cards, before)
    assert upstream.calls == []

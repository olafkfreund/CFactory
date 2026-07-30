"""Card -> factory intake + PARR status write-back (RFC-0019 Phase 3, #302).

Contract points covered:

* promoting a card to ``ready`` with a tier dispatches ONCE and joins the card
  to a work item via ``correlation_key``;
* a second promotion of the joined card does NOT re-dispatch (idempotency —
  this is the guard the mutation check weakens);
* low/medium and hard take the DIFFERENT documented routes (AIFactory build vs
  PFactory planning first);
* an inbound PARR completion event moves the joined card's status;
* a dispatch failure is surfaced (card blocked + ``ok=False`` audit entry), not
  swallowed into a card that still looks ready — and never a 500.
"""

from __future__ import annotations

import pytest
from cfactory import card_intake
from cfactory.audit import AuditStore
from cfactory.auth import reset_keystore
from cfactory.card_intake import card_status_for
from cfactory.cards import CardStore
from cfactory.config import Settings
from cfactory.models import ServiceState, WorkItem
from cfactory.store import WorkItemStore

from cards_harness import Upstream, build_client

# Not a credential: the HMAC anchor for the temp audit chain in these tests.
_TEST_HMAC = "card-intake-test-hmac"

AIF_INTAKE = card_intake.AIFACTORY_INTAKE_ENDPOINT
PF_INTAKE = card_intake.PFACTORY_INTAKE_ENDPOINT


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def items(tmp_path):
    return WorkItemStore(f"sqlite:///{tmp_path / 'items.db'}")


@pytest.fixture
def audit(tmp_path):
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret=_TEST_HMAC)


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def client(cards, items, audit, upstream, monkeypatch):
    # An intake project is configured, so a low/medium card has a build target.
    settings = Settings(intake_project_id="proj-1")
    monkeypatch.setattr(card_intake, "get_settings", lambda: settings)
    return build_client(cards, items, audit, upstream)


@pytest.fixture(autouse=True)
def _restore_keystore():
    yield
    reset_keystore()


def _card(client, **overrides) -> dict:
    body = {"title": "Ship the widget", "acceptance_criteria": ["AC#1: it ships"]}
    body.update(overrides)
    resp = client.post("/api/cards", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _promote(client, card_key: str, tier: str = "low") -> dict:
    resp = client.patch(f"/api/cards/{card_key}", json={"status": "ready", "tier": tier})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_promoting_to_ready_dispatches_once_and_joins(client, upstream):
    """The headline contract: ready + tier -> one dispatch, card joined."""
    card = _card(client)
    assert card["correlation_key"] is None

    promoted = _promote(client, card["card_key"])

    assert len(upstream.calls) == 1
    assert upstream.calls[0][0] == AIF_INTAKE
    # Joined to the work item the dispatch created, and no longer merely "ready".
    assert promoted["correlation_key"] == "task-7"
    assert promoted["status"] == "in_progress"


def test_second_promotion_does_not_redispatch(client, upstream):
    """Idempotency: a joined card re-promoted is a no-op, not a second build."""
    card = _card(client)
    _promote(client, card["card_key"])
    assert len(upstream.calls) == 1

    again = client.patch(f"/api/cards/{card['card_key']}", json={"status": "ready"})

    assert again.status_code == 200
    assert len(upstream.calls) == 1, "re-promotion double-dispatched"
    assert again.json()["correlation_key"] == "task-7"


@pytest.mark.parametrize("tier", ["low", "medium"])
def test_small_card_goes_straight_to_aifactory_build(client, upstream, tier):
    """RFC-0011 §3: low/medium skip planning and build directly."""
    card = _card(client)
    _promote(client, card["card_key"], tier=tier)

    path, payload = upstream.calls[0]
    assert path == AIF_INTAKE
    assert payload["project_id"] == "proj-1"
    assert payload["payload"]["labels"] == [f"factory:{tier}"]


def test_hard_card_routes_through_pfactory_planning(client, upstream):
    """RFC-0011 §3: hard gets PFactory's full decomposition first."""
    card = _card(client)
    _promote(client, card["card_key"], tier="hard")

    path, payload = upstream.calls[0]
    assert path == PF_INTAKE
    assert payload["title"] == "Ship the widget"
    assert "AC#1: it ships" in payload["text"]


def test_ready_without_a_tier_does_not_dispatch(client, upstream):
    """RFC-0019 §3.2 gates intake on a tier; a triage-ready card just waits."""
    card = _card(client)
    resp = client.patch(f"/api/cards/{card['card_key']}", json={"status": "ready"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert upstream.calls == []


def test_card_created_directly_as_ready_dispatches(client, upstream):
    """Intake keys off the card's state, not on which verb produced it."""
    card = _card(client, status="ready", tier="medium")

    assert len(upstream.calls) == 1
    assert card["correlation_key"] == "task-7"


def test_inbound_parr_event_updates_the_joined_card(client, cards, upstream):
    """The board is the live view: the same event stream drives the card."""
    card = _card(client)
    _promote(client, card["card_key"])

    running = client.post(
        "/api/events",
        json={
            "id": "ev-1",
            "correlation_key": "task-7",
            "service": "aifactory",
            "task_id": "task-7",
            "status": "in_progress",
        },
    )
    assert running.status_code == 200
    assert cards.get(card["card_key"]).status == "in_progress"

    done = client.post(
        "/api/events",
        json={
            "id": "ev-2",
            "correlation_key": "task-7",
            "service": "aifactory",
            "task_id": "task-7",
            "status": "completed",
        },
    )
    assert done.status_code == 200
    assert cards.get(card["card_key"]).status == "done"


def test_inbound_failure_event_blocks_the_joined_card(client, cards):
    card = _card(client)
    _promote(client, card["card_key"])

    client.post(
        "/api/events",
        json={
            "id": "ev-1",
            "correlation_key": "task-7",
            "service": "aifactory",
            "task_id": "task-7",
            "status": "failed",
        },
    )

    assert cards.get(card["card_key"]).status == "blocked"


def test_event_for_an_unjoined_correlation_touches_no_card(client, cards):
    """An ordinary GitHub-issue run must not disturb the board."""
    card = _card(client)

    client.post(
        "/api/events",
        json={
            "id": "ev-1",
            "correlation_key": "issue-99",
            "service": "aifactory",
            "task_id": "t-99",
            "status": "failed",
        },
    )

    assert cards.get(card["card_key"]).status == "backlog"


def test_dispatch_failure_is_surfaced_not_swallowed(client, cards, audit, upstream):
    """A failed dispatch blocks the card and audits ok=False — never a silent
    'ready' and never a 500 back at the caller."""
    upstream.status_code = 500
    upstream.body = {"error": "boom"}
    card = _card(client)

    resp = client.patch(f"/api/cards/{card['card_key']}", json={"status": "ready", "tier": "low"})

    assert resp.status_code == 200, "a dispatch failure must not 500 the request"
    assert resp.json()["status"] == "blocked"
    assert resp.json()["correlation_key"] is None
    dispatches = [e for e in audit.list() if e.kind == "dispatch_card"]
    assert len(dispatches) == 1
    assert dispatches[0].ok is False
    assert dispatches[0].status_code == 500
    # Blocked, not joined — so a retry after the upstream recovers still works.
    assert cards.get(card["card_key"]).correlation_key is None


def test_unconfigured_intake_project_blocks_instead_of_pretending(
    cards, items, audit, upstream, monkeypatch
):
    """No CFACTORY_INTAKE_PROJECT_ID: a low/medium card cannot be built, so it is
    blocked with that reason rather than left looking dispatched."""
    monkeypatch.setattr(card_intake, "get_settings", lambda: Settings(intake_project_id=None))
    client = build_client(cards, items, audit, upstream)

    card = _card(client)
    resp = client.patch(f"/api/cards/{card['card_key']}", json={"status": "ready", "tier": "low"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "blocked"
    assert upstream.calls == []


def _item(**stages) -> WorkItem:
    return WorkItem(
        correlation_key="k",
        **{svc: ServiceState(task_id="t", status=status) for svc, status in stages.items()},
    )


def test_a_finished_plan_is_not_a_finished_card():
    """PFactory 'done' means the BUILD is next — mapping it to `done` would lie
    on the board and then flip back."""
    assert card_status_for(_item(pfactory="completed")) == "in_progress"
    assert card_status_for(_item(pfactory="completed", aifactory="in_progress")) == "in_progress"
    assert card_status_for(_item(pfactory="completed", aifactory="completed")) == "done"
    # The verdict stage wins over an earlier green one.
    assert card_status_for(_item(aifactory="completed", tfactory="failed")) == "blocked"
    # No stage state at all yet -> nothing honest to say.
    assert card_status_for(WorkItem(correlation_key="k")) is None

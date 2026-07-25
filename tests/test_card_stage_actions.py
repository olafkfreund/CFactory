"""Explicit plan / code / test stage actions and run-the-sequence (RFC-0020 §3.7, #369).

Contract points covered:

* each stage action dispatches to the RIGHT service — plan to PFactory, code to
  AIFactory, test to TFactory — overriding tier routing for the destination while
  the tier still supplies the payload;
* an IMPOSSIBLE transition is refused with a machine-readable reason and a human
  sentence, never dispatched into nothing (this is mutation check (a));
* a sequence runs its stages IN ORDER, advancing on the same completion event that
  threads the work-item timeline, and stops on the first failure with the card
  blocked and the reason recorded;
* re-invoking is IDEMPOTENT: a completed stage is skipped, a live one refused, and
  a sequence never re-dispatches a stage that already ran (mutation check (b));
* a mid-sequence card never reads ``done`` — the honesty property, since a card
  that flashed done after planning and walked itself back would be lying;
* REST and MCP produce identical behaviour, which is a property of both going
  through ``card_ops`` rather than a coincidence between two implementations.
"""

from __future__ import annotations

import json

import pytest
from cfactory import card_intake, config, mcp
from cfactory.audit import AuditStore
from cfactory.auth import reset_keystore
from cfactory.cards import CardStore
from cfactory.config import Settings
from cfactory.store import WorkItemStore

from cards_harness import Upstream, build_client

# Not a credential: the HMAC anchor for the temp audit chain in these tests.
_TEST_HMAC = "stage-actions-test-hmac"

PLAN_DOOR = card_intake.PFACTORY_INTAKE_ENDPOINT
CODE_DOOR = card_intake.AIFACTORY_INTAKE_ENDPOINT
TEST_DOOR = card_intake.TFACTORY_INTAKE_ENDPOINT

# The MCP twin of each REST action, so every behavioural assertion below can be
# re-run over the other transport without restating it.
MCP_TOOL = {
    "plan": "cfactory_plan_card",
    "code": "cfactory_code_card",
    "test": "cfactory_test_card",
    "run": "cfactory_run_card",
}
MCP_AUTH = {"Authorization": "Bearer stage-secret"}


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
    """One TestClient serving BOTH surfaces over one set of stores and one mock
    upstream, so every behavioural claim can be re-asserted over MCP without a
    second harness that could drift from this one."""
    # An intake project is configured, so the code and test doors have a target.
    monkeypatch.setattr(card_intake, "get_settings", lambda: Settings(intake_project_id="proj-1"))
    # MCP resolves its own collaborators (no Depends), so patch the seams it calls.
    monkeypatch.setattr(mcp, "cards_store_dep", lambda _tenant=None: cards)
    monkeypatch.setattr(mcp, "get_audit_store", lambda: audit)
    monkeypatch.setattr(mcp, "action_transport_dep", upstream.transport)
    # The legacy full-scope bearer, so the MCP twins are reachable in one line.
    monkeypatch.setenv("CFACTORY_MCP_SECRET", "stage-secret")
    monkeypatch.setattr(config, "_settings", None)
    return build_client(cards, items, audit, upstream)


@pytest.fixture(autouse=True)
def _restore_keystore():
    yield
    reset_keystore()


# ── helpers ───────────────────────────────────────────────────────────────────


def _card(client, **overrides) -> dict:
    body = {
        "title": "Ship the widget",
        "acceptance_criteria": ["AC#1: it ships"],
        "tier": "low",
    }
    body.update(overrides)
    resp = client.post("/api/cards", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def act(client, card_key: str, action: str):
    """Invoke one stage action over REST."""
    return client.post(f"/api/cards/{card_key}/actions/{action}")


def act_mcp(client, card_key: str, action: str) -> dict:
    """Invoke the same action over MCP, returning the tool's decoded payload."""
    resp = client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": MCP_TOOL[action], "arguments": {"card_key": card_key}},
        },
    )
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


def complete(client, service: str, event_id: str, status: str = "completed", key: str = "task-7"):
    """Feed the completion event that settles a stage (and advances a sequence)."""
    resp = client.post(
        "/api/events",
        json={
            "id": event_id,
            "correlation_key": key,
            "service": service,
            "task_id": "task-7",
            "status": status,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


# ── each stage goes to the right service ──────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "door", "service"),
    [("plan", PLAN_DOOR, "pfactory"), ("code", CODE_DOOR, "aifactory")],
)
def test_stage_action_dispatches_to_its_own_service(client, upstream, action, door, service):
    """plan -> PFactory, code -> AIFactory, whatever the tier says."""
    card = _card(client)

    resp = act(client, card["card_key"], action)

    assert resp.status_code == 200, resp.text
    assert upstream.paths == [door]
    body = resp.json()
    assert body["stage"]["target_service"] == service
    assert body["stage"]["dispatched"] is True
    assert body["card"]["stage_runs"][action]["status"] == "dispatched"


def test_test_stage_dispatches_to_tfactory_once_a_build_exists(client, upstream):
    """The third door, reached only after a build completed."""
    card = _card(client)
    act(client, card["card_key"], "code")
    complete(client, "aifactory", "ev-code")

    resp = act(client, card["card_key"], "test")

    assert resp.status_code == 200, resp.text
    assert upstream.paths == [CODE_DOOR, TEST_DOOR]
    assert resp.json()["stage"]["target_service"] == "tfactory"
    # TFactory's spec-ingest door keys the verification workspace on spec_id and
    # generates lanes from the spec text, so the card's own key + brief go through.
    payload = upstream.payload_for(TEST_DOOR)
    assert payload["project_id"] == "proj-1"
    assert payload["spec_id"] == card["card_key"]
    assert "AC#1: it ships" in payload["spec_text"]


def test_explicit_plan_overrides_tier_routing_and_says_so(client, upstream):
    """A `low` card would skip planning; asking for it plans anyway, with a warning
    naming the override rather than a silent surprise."""
    card = _card(client, tier="low")

    resp = act(client, card["card_key"], "plan")

    assert upstream.paths == [PLAN_DOOR]
    assert any(w.startswith("tier_override") for w in resp.json()["stage"]["warnings"])


def test_coding_a_hard_card_with_no_plan_warns_that_a_stage_was_skipped(client, upstream):
    """Allowed, not refused — building without a plan is a lossy choice a human is
    entitled to make, so it is surfaced instead of blocked."""
    card = _card(client, tier="hard")

    resp = act(client, card["card_key"], "code")

    assert resp.status_code == 200
    assert upstream.paths == [CODE_DOOR]
    assert any(w.startswith("plan_skipped") for w in resp.json()["stage"]["warnings"])


# ── impossible transitions are refused, never dispatched ──────────────────────


def test_testing_a_card_that_was_never_built_is_refused(client, upstream):
    """MUTATION CHECK (a): break the precondition and this test catches TFactory
    being asked to verify a build that does not exist."""
    card = _card(client)

    resp = act(client, card["card_key"], "test")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "no_build_to_verify"
    assert "never built" in resp.json()["detail"]["message"] or "completed build" in (
        resp.json()["detail"]["message"]
    )
    assert upstream.calls == [], "a refused transition must not reach any upstream"


def test_testing_a_card_whose_build_is_still_running_is_refused(client, upstream):
    """A dispatched-but-unfinished build is not a build to verify."""
    card = _card(client)
    act(client, card["card_key"], "code")

    resp = act(client, card["card_key"], "test")

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "no_build_to_verify"
    assert upstream.paths == [CODE_DOOR]


def test_a_card_with_no_tier_is_refused_for_every_action(client, upstream):
    """The tier supplies the payload, so there is nothing to send without one —
    the same rule the implicit promotion path applies."""
    card = _card(client, tier=None)

    for action in ("plan", "code", "test", "run"):
        resp = act(client, card["card_key"], action)
        assert resp.status_code == 409, action
        assert resp.json()["detail"]["reason"] in ("no_tier", "no_build_to_verify"), action
    assert upstream.calls == []


def test_a_live_stage_refuses_a_second_press(client, upstream):
    """The double-press guard: one dispatch, not two."""
    card = _card(client)
    assert act(client, card["card_key"], "code").status_code == 200

    resp = act(client, card["card_key"], "code")

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "stage_already_running"
    assert upstream.paths == [CODE_DOOR], "the second press dispatched again"


def test_code_without_an_intake_project_is_refused_with_the_reason(
    cards, items, audit, upstream, monkeypatch
):
    """Both the code and test doors need a project_id the card has not got, so an
    unconfigured cockpit is told that rather than dispatching into nothing."""
    monkeypatch.setattr(card_intake, "get_settings", lambda: Settings(intake_project_id=None))
    client = build_client(cards, items, audit, upstream)
    card = _card(client)

    resp = act(client, card["card_key"], "code")

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "no_intake_project"
    assert upstream.calls == []


def test_an_action_on_an_unknown_card_is_a_404(client):
    assert act(client, "FCT-nope", "plan").status_code == 404


# ── idempotency ───────────────────────────────────────────────────────────────


def test_a_completed_stage_is_skipped_not_rerun(client, upstream):
    """Re-invoking is a no-op with a reason, exactly as re-promoting a dispatched
    card is."""
    card = _card(client)
    act(client, card["card_key"], "code")
    complete(client, "aifactory", "ev-code")

    resp = act(client, card["card_key"], "code")

    assert resp.status_code == 200
    assert resp.json()["stage"]["dispatched"] is False
    assert resp.json()["stage"]["skipped"] == "stage_already_complete"
    assert upstream.paths == [CODE_DOOR], "a completed stage was dispatched twice"


def test_the_correlation_key_is_written_once_and_reused_by_later_stages(client, cards):
    """Write-once preserved: planning sets the key, and the build threads the SAME
    one rather than re-pointing the card at a new correlation."""
    card = _card(client, tier="hard")
    act(client, card["card_key"], "plan")
    joined = cards.get(card["card_key"]).correlation_key
    assert joined == "task-7"

    complete(client, "pfactory", "ev-plan")
    act(client, card["card_key"], "code")

    assert cards.get(card["card_key"]).correlation_key == joined


# ── the sequence ──────────────────────────────────────────────────────────────


def test_run_walks_plan_then_code_then_test_in_order(client, upstream, cards):
    """The whole point: one press, three stages, each advancing on the completion
    event that finished the previous one — no second orchestrator."""
    card = _card(client, tier="hard")

    resp = act(client, card["card_key"], "run")
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"]["sequence"] == ["plan", "code", "test"]
    assert upstream.paths == [PLAN_DOOR], "run must dispatch only the first stage"

    complete(client, "pfactory", "ev-plan")
    assert upstream.paths == [PLAN_DOOR, CODE_DOOR]

    complete(client, "aifactory", "ev-code")
    assert upstream.paths == [PLAN_DOOR, CODE_DOOR, TEST_DOOR]

    complete(client, "tfactory", "ev-test")
    runs = cards.get(card["card_key"]).stage_runs
    assert [runs[s]["status"] for s in ("plan", "code", "test")] == ["done", "done", "done"]
    assert cards.get(card["card_key"]).status == "done"


def test_a_sequenced_card_never_reads_done_before_its_last_stage(client, cards):
    """THE HONESTY PROPERTY. A completed stage is not a verdict while later stages
    are still owed: a card that flashed `done` the moment planning finished and
    then walked itself back when coding started would be lying to the board."""
    card = _card(client, tier="hard")
    act(client, card["card_key"], "run")

    complete(client, "pfactory", "ev-plan")
    assert cards.get(card["card_key"]).status == "in_progress"

    complete(client, "aifactory", "ev-code")
    assert cards.get(card["card_key"]).status == "in_progress", (
        "a build completing mid-sequence is not a verdict — test has not run"
    )

    complete(client, "tfactory", "ev-test")
    assert cards.get(card["card_key"]).status == "done"


def test_run_does_not_redispatch_a_stage_that_already_ran(client, upstream, cards):
    """MUTATION CHECK (b): break sequence idempotency and this test catches the
    double dispatch — `run` on a card whose plan is already complete must resume
    at code, not plan it again."""
    card = _card(client, tier="hard")
    act(client, card["card_key"], "plan")
    complete(client, "pfactory", "ev-plan")
    assert upstream.paths == [PLAN_DOOR]

    resp = act(client, card["card_key"], "run")

    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"]["sequence"] == ["code", "test"]
    assert upstream.paths == [PLAN_DOOR, CODE_DOOR], "run re-planned an already-planned card"
    assert cards.get(card["card_key"]).stage_runs["plan"]["status"] == "done"


def test_pressing_run_twice_dispatches_once(client, upstream):
    """Concurrent double-press: the second press finds a live stage and refuses."""
    card = _card(client)
    assert act(client, card["card_key"], "run").status_code == 200

    resp = act(client, card["card_key"], "run")

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "stage_already_running"
    assert len(upstream.calls) == 1, "pressing run twice dispatched twice"


def test_a_failed_stage_stops_the_sequence_blocks_the_card_and_says_why(
    client, upstream, cards, audit
):
    """Fail-safe: the sequence stops, the card is blocked with the reason on the
    dispatch record, and the remaining stages stay queued so `run` can resume."""
    card = _card(client, tier="hard")
    act(client, card["card_key"], "run")

    complete(client, "pfactory", "ev-plan", status="failed")

    row = cards.get(card["card_key"])
    assert row.status == "blocked"
    assert row.stage_runs["plan"]["status"] == "failed"
    assert row.stage_runs["plan"]["detail"] == "failed"
    assert row.stage_runs["code"]["status"] == "queued", "the sequence must be resumable"
    assert upstream.paths == [PLAN_DOOR], "the sequence advanced past a failure"

    # And it resumes rather than restarting.
    resume = act(client, card["card_key"], "run")
    assert resume.json()["stage"]["sequence"] == ["plan", "code", "test"]
    assert upstream.paths == [PLAN_DOOR, PLAN_DOOR]


def test_a_dispatch_failure_blocks_the_card_and_is_surfaced_never_a_500(client, upstream, audit):
    """An unreachable factory is a blocked card with a reason and an ok=False audit
    entry — never a silent 'ready', never a 500."""
    card = _card(client)
    upstream.fail_at(CODE_DOOR)

    resp = act(client, card["card_key"], "code")

    assert resp.status_code == 200, "a dispatch failure must not 500 the request"
    assert resp.json()["stage"]["ok"] is False
    assert "dispatch failed" in resp.json()["stage"]["reason"]
    assert resp.json()["card"]["status"] == "blocked"
    assert resp.json()["card"]["stage_runs"]["code"]["status"] == "failed"
    failures = [e for e in audit.list() if e.kind == "dispatch_card" and not e.ok]
    assert len(failures) == 1
    assert failures[0].status_code == 500


def test_every_stage_action_is_audit_chained(client, audit):
    """Same chain, same shape as any other confirmed write."""
    card = _card(client, tier="hard")
    act(client, card["card_key"], "plan")

    dispatches = [e for e in audit.list() if e.kind == "dispatch_card"]
    assert len(dispatches) == 1
    assert dispatches[0].target_service == "pfactory"
    assert dispatches[0].ok is True
    assert audit.verify() == [], "the audit chain must stay intact"


def test_a_sequence_advance_is_audited_too(client, audit):
    """A stage nobody typed still leaves an entry — the board's history must not
    have a hole where the automatic advance was."""
    card = _card(client, tier="hard")
    act(client, card["card_key"], "run")
    complete(client, "pfactory", "ev-plan")

    # audit.list() is newest-first, so reverse it to read the chain in order.
    dispatches = [e for e in reversed(audit.list()) if e.kind == "dispatch_card"]
    assert [e.target_service for e in dispatches] == ["pfactory", "aifactory"]
    assert dispatches[1].actor == "cfactory-sequence"


# ── REST and MCP agree ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("action", "door"), [("plan", PLAN_DOOR), ("code", CODE_DOOR)])
def test_mcp_stage_tool_dispatches_through_the_same_door_as_rest(client, upstream, action, door):
    card = _card(client)

    payload = act_mcp(client, card["card_key"], action)

    assert upstream.paths == [door]
    assert payload["stage"]["dispatched"] is True
    assert payload["card"]["stage_runs"][action]["status"] == "dispatched"


def test_mcp_refuses_an_impossible_transition_with_the_same_reason_as_rest(client, upstream):
    """Parity is about behaviour, not just about a tool existing: the refusal codes
    must match, because they both come from the one rule set in card_ops."""
    card = _card(client)

    payload = act_mcp(client, card["card_key"], "test")

    assert payload["reason"] == "no_build_to_verify"
    assert upstream.calls == []


def test_mcp_run_is_idempotent_exactly_as_rest_is(client, upstream):
    card = _card(client)
    act_mcp(client, card["card_key"], "run")

    second = act_mcp(client, card["card_key"], "run")

    assert second["reason"] == "stage_already_running"
    assert len(upstream.calls) == 1


def test_a_stage_dispatched_over_mcp_is_visible_over_rest(client, cards):
    """One store, one dispatch record: the surfaces cannot disagree about what the
    factory was asked to do."""
    card = _card(client)
    act_mcp(client, card["card_key"], "code")

    over_rest = client.get(f"/api/cards/{card['card_key']}").json()

    assert over_rest["stage_runs"]["code"]["status"] == "dispatched"
    assert over_rest["stage_runs"]["code"]["service"] == "aifactory"
    assert over_rest["correlation_key"] == "task-7"

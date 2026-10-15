---
status: draft
issue: 478
intent: intent/2026-10-15-478-aifactory-card-join.md
---

# Spec: a board-dispatched card settles when AIFactory finishes CODE

## Design

Derive the card's join key from an AIFactory intake response the same way
AIFactory derives the key of its own completion event, so the two sides agree
by construction.

`_upstream_key` (`apps/backend/cfactory/card_intake.py:498`) gains the target
service. For `aifactory`, when the body has no explicit `correlation_key` but
has a `spec_id`, it returns `str(issue_number)` when `issue_number` is present,
else `f"af-{spec_id}"`. This mirrors AIFactory `correlation_key(spec_id,
issue_number)` in `apps/web-server/server/services/completion.py`; the
`issue_number` returned by from-issue is the same value from-issue writes into
`requirements.json` as `githubIssue.number`, which is what the event reads.

An explicit `correlation_key` in any response still wins first. Every other
service and every other response shape falls through to the existing key list
unchanged. The single call site (`_send`, `card_intake.py:553`) passes
`action.target_service`.

The write-once rule at `:558` is unchanged: only the first stage sets the key.

## Alternatives rejected

- Look the card up by any stage's `ref` in `apply_status`: a second join path
  that every ingress would have to share; larger, and the key would still be
  wrong in `GET /api/cards`.
- Ask AIFactory to return `correlation_key` from from-issue: the right long-term
  contract, but a cross-repo change; the derivation here keeps working after it
  lands because an explicit `correlation_key` wins.
- Normalise the event key on ingest: CFactory cannot map `af-<spec>` back to
  `<project>:<spec>` without the project id.

## Risks

- If AIFactory ever changes its key rule, the two drift again. The explicit
  `correlation_key` path is the escape hatch; the regression test pins the rule.
- Cards already dispatched keep their old key (write-once); they do not heal.
  Acceptable: they can be re-dispatched.

## Verification

A wiring test in `tests/test_card_stage_actions.py`: the mock AIFactory door
returns a from-issue-shaped body (`task_id "proj-1:003-x"`, `spec_id`,
`issue_number None`), `code` is dispatched, then an AIFactory completion event
keyed `af-003-x` arrives; the CODE run must be `done`. Run against the unfixed
code it must fail. Full `tests/test_card_stage_actions.py` and
`tests/test_card_intake*.py` stay green; ruff and mypy --strict on the changed
file add no violations.

---
status: draft
issue: 478
spec: spec/2026-10-15-478-aifactory-card-join.md
---

# Plan: a board-dispatched card settles when AIFactory finishes CODE

Self-contained summary of the approved decisions:

- `_upstream_key(body, service)` in `apps/backend/cfactory/card_intake.py`
  derives the join key. An explicit `correlation_key` in the body still wins.
  For `service == "aifactory"` with a `spec_id` in the body it returns
  `str(issue_number)` when `issue_number` is not None, else `f"af-{spec_id}"`,
  mirroring AIFactory `services/completion.py` `correlation_key()`. Every other
  case keeps the existing key list (`task_id`, `session_id`, `id`, `spec_id`).
- The one caller, `_send`, passes `action.target_service`.
- Write-once correlation key (`:558`) unchanged. Already-dispatched cards do not
  heal (accepted).
- Out of scope: a PLAN-first card's later AIFactory event; AIFactory returning
  an explicit `correlation_key`.

## Steps

1. `tests/test_card_stage_actions.py` (after the existing
   `test_test_stage_dispatches_to_tfactory_once_a_build_exists`): add
   `test_code_stage_settles_on_aifactory_shaped_completion`. Set
   `upstream.overrides[CODE_DOOR] = (200, {"task_id": "proj-1:003-x",
   "spec_id": "003-x", "issue_number": None})`, dispatch `code`, then
   `complete(client, "aifactory", "ev-af", key="af-003-x")`; assert
   `cards.get(key).stage_runs["code"]["status"] == "done"` and the card
   correlation key is `af-003-x`. Add a second case with `issue_number: 42`
   and event key `"42"` (parametrize).
   → verify it FAILS on unfixed code:
   `PYTHONPATH=apps/backend pytest -q tests/test_card_stage_actions.py -k aifactory_shaped`
   Traps: the shared `Upstream` default body is `{"task_id": "task-7"}`; do not
   change it, other tests depend on it.
2. `apps/backend/cfactory/card_intake.py:498-512` and `:553`: add the `service`
   parameter and the AIFactory branch; pass `action.target_service` at the call
   site. Update the docstring.
   → verify step 1 passes, then
   `PYTHONPATH=apps/backend pytest -q tests/test_card_stage_actions.py tests/test_card_intake.py tests/test_card_intake_issue_number.py`
   Traps: no emojis; `mypy --strict` ratchet on the changed file; stage named
   paths only (never `git add -A`).

## Tests

- The targeted pytest files above: all pass.
- `ruff check` and `ruff format --check` on the two changed files: clean.
- `mypy --strict apps/backend/cfactory/card_intake.py`: no new errors vs base.

## Rollback

Revert the implementation commit; no data migration, keys already written stay.

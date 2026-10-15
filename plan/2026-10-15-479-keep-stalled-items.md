---
status: draft
issue: 479
also_closes: 480
spec: spec/2026-10-15-479-keep-stalled-items.md
---

# Plan: a stalled task stays on the board as stalled, not deleted

Self-contained summary of the approved decisions:

- The poller no longer deletes work-item rows. `prune_stuck` and
  `prune_stalled` are removed with their calls in `poll_progress_once`.
- Stuck/stalled upstream items stay out of the live progress hub, but are
  hydrated into the store and are part of the live set for
  `reconcile_snapshot`, so the stage is stored as `stalled` and history is kept.
- A silent running row keeps its `updated_at` (existing #105 no-op-poll rule);
  `compute_liveness` flags it stalled after the deadline. `compute_liveness` is
  NOT changed.
- `_plan_child_state` returns `stalled` for any stage status containing `stall`
  or `stuck`, checked after failure and before done.
- `reconcile_snapshot`, `prune_duplicate_stages`, cards, `_contracts`: unchanged.
- Product change (reverses #186/#187 auto-remove): flagged for the reviewer.
- One PR closes #479 and #480.

## Steps

1. `tests/test_prune_stuck.py`: rewrite to the new behaviour.
   - Module docstring: stalled tasks are kept and stored as stalled.
   - Delete the four `prune_stuck` unit tests (`:43-72`) and the four
     `prune_stalled` unit tests (`:162-191`).
   - `test_poll_auto_removes_stalled_and_keeps_healthy` becomes
     `test_poll_keeps_stalled_out_of_the_hub_but_in_the_store`: `store.get("2")`
     is not None with `aifactory.status == "stalled"`; hub still excludes it.
   - `test_poll_deletes_a_task_that_later_stalls` becomes
     `test_poll_keeps_a_task_that_later_stalls`: row kept, status `stalled`.
   - New #479 wiring test: store a child (issue 9) with AIFactory `completed`,
     then poll a TFactory-shaped... (use the AIFactory mock adapter for the
     stalled stage is wrong service) -> use `upsert_snapshot` for AIFactory
     `completed` and a `TFactoryAdapter` mock reporting the TFactory task
     `stalled` for issue 9 through `poll_progress_once`; then
     `task_process._build_plan_graph(session, store)` with a session
     `{"board_state": "done", "epic": {"children": [{"key": "a"}]},
     "emit_result": {"child_numbers": {"a": 9}}}` must give node status
     `stalled`.
   - New #480 wiring test: poll an `in_progress` item, then poll the same list
     item again; then `compute_liveness(store.get(key), now=+1h).stalled` is
     True and `updated_at` equals the first poll's.
   → verify the new/changed tests FAIL on unfixed code:
   `PYTHONPATH=apps/backend pytest -q tests/test_prune_stuck.py`
   Traps: check the TFactory adapter's list JSON shape in
   `apps/backend/cfactory/adapters/tfactory.py` before writing the mock.
2. `apps/backend/cfactory/progress.py:136-171`: keep `stuck_ids` only for the
   hub skip; `live = items`; drop the `prune_stuck` call and its comment; drop
   the `prune_stalled()` block. Update `_is_dead_stage`'s docstring ("kept out
   of the live feed", not "auto-removed").
   → verify `pytest -q tests/test_prune_stuck.py tests/test_progress.py`
3. `apps/backend/cfactory/store.py:633-682`: delete `prune_stuck` and
   `prune_stalled`; drop `is_running` from the import at `:24` if unused.
   → verify `ruff check apps/backend/cfactory` clean.
4. `apps/backend/cfactory/task_process.py:108-116`: add after the failure
   branch `elif any(w in s for s in statuses for w in ("stall", "stuck")):
   status = "stalled"`.
   → verify the #479 test passes.
5. `apps/backend/cfactory/cards.py:10-11`, `docs/guides/planning-board.md:66`:
   remove `prune_stuck`/`prune_stalled` from the lists of deleting machinery.
   → verify `git grep -n "prune_stuck\|prune_stalled"` returns nothing outside
   `intent/ spec/ plan/`.
   Traps (all steps): no emojis; stage named paths only; do not reformat
   unrelated hunks (local ruff may differ from CI's pinned version, check
   `git diff`); mypy --strict ratchet on changed files.

## Tests

- `PYTHONPATH=apps/backend pytest -q` (whole backend suite): green.
- `ruff check apps/backend/cfactory scripts`, `ruff format --check` on changed
  files: clean.
- `mypy --strict` on changed backend files: no new errors vs origin/dev.

## Rollback

Revert the implementation commits. No schema or data migration.

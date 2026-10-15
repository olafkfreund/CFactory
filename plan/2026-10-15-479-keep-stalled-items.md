---
status: approved
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
   - New #479 wiring test: `upsert_snapshot("9", AIFACTORY, completed)`, then
     `poll_progress_once` with a `TFactoryAdapter` mock whose list
     (`{"tasks": [...]}` on `/api/tfactory/tasks`) reports
     `{"spec_id": "tf-9", "status": "stalled", "github_issue": 9}`; then
     `task_process._build_plan_graph(session, store)` with
     `{"board_state": "done", "epic": {"children": [{"key": "a"}]},
     "emit_result": {"child_numbers": {"a": 9}}}` must give node status
     `stalled`.
   - New #480 wiring test: poll an `in_progress` AIFactory item; set
     `CFACTORY_STALL_DEADLINE_SECONDS=0.001` (monkeypatch) and sleep 0.01 s;
     poll the same list item twice more (on unfixed code the first deletes the
     row, the second re-creates it fresh). The row's `updated_at` must equal
     the first poll's and `compute_liveness(row).stalled` must be True.
   → verify the new/changed tests FAIL on unfixed code:
   `PYTHONPATH=apps/backend pytest -q tests/test_prune_stuck.py`
   Traps: TFactory keys on `spec_id` and the issue on `github_issue`
   (`adapters/tfactory.py:117-138`).
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

## Deviation (after review)

The Opus review of the diff found two gaps, fixed in one follow-up commit:

- Step 4's check used substrings (`"stall" in s`), so `installed` read as
  stalled. It now calls `progress._is_dead_stage` (whole-token match). Test:
  `test_plan_child_stall_check_is_whole_token`.
- The cockpit's `stageState` (`apps/frontend-web/src/taskState.ts`) mapped a
  kept `stalled` stage to `running`, which #186/#187 deleted rows to avoid.
  A whole-token `stalled`/`stuck` now reads `failed`. Test:
  `apps/frontend-web/src/taskState.test.ts`.

Not changed (out of scope, noted for the reviewer): the backend card
write-back (`card_status_for`) still maps `stalled` to `in_progress`, because
`is_stuck` only knows `stuck` (audit note N1).

## Deviation (lead review of #483, item 1): needs-you list and count agree

Folding `stalled` into `failed` made `attentionFor` (`needsYou.ts`) return null
for a liveness-stalled row, because it only flagged an overall `running` item,
while the backend `/api/needs-you/count` (`needs_you.py`) still counted it.
`taskState.ts` now exports `isStalledStatus`; `attentionFor` flags a
liveness-stalled row whose stage is stalled and names that stage. Tests on the
same row (AIFactory completed, TFactory stalled, 1 h old):
`needsYou.test.ts` and `tests/test_needs_you.py`.

## Deviation (lead review of #483, item 2): reconcile keeps a stalled stage

`reconcile_snapshot` (`store.py`) blanked any non-terminal stage the upstream
stopped listing, and `stalled` is not terminal, so the plan node went back to
`completed` once TFactory pruned the stalled spec. It now skips a dead stage
too. To use the check from `store.py` without an import cycle,
`_is_dead_stage` moved from `progress.py` to `status_taxonomy.is_dead_stage`
(`progress.py` keeps the old name as an import alias; `task_process.py` imports
it from the taxonomy). Test: `test_stalled_stage_survives_the_upstream_dropping_it`.

## Deviation (user decision on #483, 2026-10-15): delayed auto-removal

The user chose option (b): dead stalled cards are still removed automatically,
but only after a delay, instead of immediately (#186/#187) or never.

- `store.py` `prune_dead(now=, max_age_seconds=)`: deletes a row whose
  current (furthest) stage is dead (`status_taxonomy.is_dead_stage`) and whose
  `updated_at` is older than the limit; logs each removal at info with the key
  and task id. A row whose current stage is running is never removed here,
  however old.
- Limit: `stalled_prune_seconds()`, env `CFACTORY_STALLED_PRUNE_HOURS`,
  default 24 h, same pattern as `CFACTORY_STALL_DEADLINE_SECONDS`; documented
  in `.env.example` and `techdocs/dependencies.md`.
- `progress.py` `poll_progress_once` calls `store.prune_dead()` once per cycle,
  after every adapter, where the old `prune_stalled()` ran.
- Known limit (shortcut comment in code): if the upstream still lists the dead
  task, the next poll re-creates the row with a fresh clock, so it shows again
  for another limit.
- Tests in `tests/test_prune_stuck.py`: 23 h stalled kept, 25 h stalled
  removed, running row at 1000 h kept.

## Deviation (lead re-review of #483): a re-run is not overwritten by its dead task

Hydrating every listed item (`live = items`, step 2) let a stalled task and
its completed re-run, which share an issue number and so one row, both write
it; the last in the list won, and `updated_at` moved every poll, so the row
never aged. `poll_progress_once` now drops a dead task from the hydrate and
reconcile set when the same correlation key also has a task that is not dead.
Test: `test_rerun_of_a_stalled_task_ends_completed`, list orders `[new, old]`
and `[old, new]`, both end `completed` on task `new`, and repeated polls
leave `updated_at` unchanged so the row still ages. Without the fix `[new, old]`
ends `stalled` and `[old, new]` moves `updated_at` every poll.

## Deviation (lead final review of #483): one writer per row, and no takeover

- Two dead tasks on one key (a task and its re-run both dead, different
  phases) both wrote the row every poll, so it never aged or got pruned.
  `poll_progress_once` now hydrates only the last dead task listed for a key
  with no live task.
- A stalled original could take the row back from its completed re-run once
  the upstream stopped listing the re-run, and `prune_dead` would then delete
  the row with its history. `upsert_snapshot` (`store.py`) now skips a write
  whose status is dead when the stage already holds a done status from a
  different task_id. Trade-off: a later, different task that stalls after an
  earlier one completed on the same key keeps showing the completed one.
- The poller's own `prune_dead` call is now covered by a `poll_progress_once`
  test.

Tests: `test_two_dead_tasks_on_one_key_still_age_and_are_pruned`,
`test_dead_original_does_not_take_the_row_back_from_a_finished_rerun`,
`test_the_poll_itself_removes_an_aged_dead_row`; each fails without its fix.

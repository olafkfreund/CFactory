---
status: approved
issue: 479
also_closes: 480
intent: intent/2026-10-15-479-keep-stalled-items.md
---

# Spec: a stalled task stays on the board as stalled, not deleted

## Design

Stop the poller deleting rows, and let the state it already has flow to the
readers.

1. `progress.py` `poll_progress_once` (`:136-171`): stuck/stalled items stay out
   of the live progress hub (it is the running feed) but are hydrated into the
   store and count as live for `reconcile_snapshot`. The `store.prune_stuck`
   call and the `store.prune_stalled()` sweep are removed. The stored stage then
   reads `stalled`, and the row keeps its earlier slices, timeline and usage.
2. `store.py`: delete `prune_stuck` and `prune_stalled` (no callers left), and
   the `is_running` import if unused. The #105 rule in `upsert_snapshot` already
   skips a no-op poll, so a silent row keeps its `updated_at`, and
   `compute_liveness` flags it stalled after the deadline; `stalled_count`,
   `needs_you.py` and the 24 h stuck anomaly read that.
3. `task_process.py` `_plan_child_state` (`:94-116`): after the failure check,
   a status containing `stall` or `stuck` makes the node `stalled` (the status
   `TaskFlow.tsx:71` already colours amber), so a child whose AIFactory stage is
   `completed` and TFactory stage `stalled` no longer reads `completed`.
4. Docs that name the removed functions (`cards.py` module docstring,
   `docs/guides/planning-board.md`) drop the two names; `_is_dead_stage`'s
   docstring no longer says "auto-removed".

One fix closes both issues: #480's delete/re-hydrate cycle cannot happen when
nothing is deleted.

## Alternatives rejected

- Mark the row stalled instead of deleting (in `prune_stalled`): the next poll
  rewrites the upstream's `in_progress` over the mark and resets `updated_at`,
  so it needs a second rule in `upsert_snapshot`. More code, same reader result
  as letting liveness age the untouched row.
- Keep `prune_stalled` only for rows the poll did not list: every listed
  service's unlisted non-terminal stages are already blanked by
  `reconcile_snapshot`, so what is left is the #480 case or an adapter outage,
  where deleting is also wrong.
- Flag an upstream `stalled` marker as `liveness.stalled` immediately: useful,
  but it changes `compute_liveness` for every reader; the deadline already
  surfaces it. Left out.

## Risks

- Product change: stalled tasks no longer vanish from the board (#186/#187).
  They show stalled until removed (existing Remove action) or until the upstream
  stops listing them, when `reconcile_snapshot` blanks the stage as before.
- An event-sourced row with no poll match and a silent running stage now stays
  and reads stalled instead of being deleted. Intended.

## Verification

`tests/test_prune_stuck.py`:
- the two poll tests that asserted deletion now assert the row is kept with
  stage status `stalled` and absent from the hub;
- #479 wiring: a plan child with AIFactory `completed` then TFactory `stalled`
  through `poll_progress_once`; `_build_plan_graph` with `plan_done` must give
  the node `stalled`, not `completed`;
- #480 wiring: hydrate an `in_progress` item, poll again with the same list item
  after the deadline; the row keeps its old `updated_at` and
  `compute_liveness(...).stalled` is True.
- the store-level `prune_stuck`/`prune_stalled` unit tests are removed with the
  functions.
Each new test fails on origin/dev. Full backend pytest, ruff, ruff format, and
the mypy ratchet on changed files stay green.

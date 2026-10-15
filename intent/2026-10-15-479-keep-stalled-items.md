---
status: draft
issue: 479
also_closes: 480
author: olafkfreund
---

# Intent: a stalled task stays on the board as stalled, not deleted

## Problem

With `CFACTORY_LIVE_PROGRESS=true` (the deployed setting) the poller deletes
work-item rows it still sees upstream, and every reader then reports the wrong
state. Two paths, one cause ("deletion presented as success", Factory#971 audit):

- #479: when TFactory's sweep marks a hung stage `stalled`, `poll_progress_once`
  (`apps/backend/cfactory/progress.py:136-147`) calls `store.prune_stuck`
  (`store.py:633-657`), which deletes the whole row, earlier `done` slices
  included. In the parent plan's DAG, `store.get(issue)` is then `None` and the
  `plan_done` fallback paints the node `completed` (`task_process.py:179-188`).
  The needs-you badge and the anomalies view lose the item.
- #480: when an executor dies while the upstream list still says `in_progress`,
  `store.prune_stalled` (`progress.py:171`, `store.py:659-682`) deletes the row
  after 900 s, and the next poll's `hydrate` re-creates it with a fresh
  `updated_at`. `stalled_count`, the needs-you badge and the 24 h stuck anomaly
  never fire; timeline and usage are wiped each cycle.

Both deletions are silent.

## Proposed outcome

A task the upstream still lists stays in the store with its history. A stage the
upstream marks `stalled` is stored as `stalled` and its plan-DAG node shows
`stalled`, never `completed`. A silent running stage keeps its stall age, so
liveness, needs-you and the stuck anomaly report it.

## Affected users and systems

CFactory backend poller (`progress.py`, `store.py`), plan DAG
(`task_process.py`, `TaskFlow.tsx`), Running Tasks / needs-you / anomalies.

## Constraints

- No deletion of a row the upstream still reports.
- `reconcile_snapshot` and `prune_duplicate_stages` stay as they are.
- Cards are not touched (separate table).
- No change to the vendored `_contracts`.

## Open questions

This reverses the auto-remove behaviour added in #186 and #187: stalled tasks
now stay visible (flagged stalled) until a person removes them or the upstream
stops listing them. Chosen as the conservative option for the bulk run; flagged
for the reviewer.

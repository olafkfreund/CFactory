---
status: draft
issue: 454
intent: intent/2026-09-23-454-stale-stuck-anomalies.md
---

# Spec: stuck anomalies must reflect current upstream status

Decisions carried from the approved intent's open questions:

1. Fix the detector only. The poll keeps writing stage slices without timeline
   events; the timeline stays a record of events actually received.
2. Items waiting on a human leave the anomaly feed. `needs_you.py`
   (`needs_human`, `/api/needs-you/count`) already counts them.

## Design

One change in `apps/backend/cfactory/copilot/anomalies.py`, section 3 of
`_detect_for_item` (the "stuck / stale" check). Sections 1 (failure) and 2
(handback loop) stay as they are.

Today the check reads `wi.timeline[-1]`: its age and its status. The
replacement reads the item's current state through the existing
`compute_liveness` in `apps/backend/cfactory/store.py`:

```python
liveness = compute_liveness(wi, now=now, deadline_seconds=stale_seconds)
if liveness.stalled and not is_review(liveness.active_status):
    hours = int(liveness.last_activity_age_seconds // 3600)
    found.append(Anomaly(
        "stuck", "medium", wi.correlation_key, wi.title,
        f"no progress for ~{hours}h (last: {liveness.active_service}={liveness.active_status})",
    ))
```

Why `compute_liveness`:

- It already picks the furthest-along stage slice (pfactory, then aifactory,
  then tfactory) and reads that slice's *current* status, which is the value the
  poll keeps up to date. A slice that the poll moved to `done` is terminal, so
  the item is not active and cannot be stuck. This fixes fault 1.
- Its age comes from `WorkItem.updated_at`. The poll path in `store.py`
  (around line 543) deliberately skips the write when status, phase and usage
  are unchanged, so `updated_at` holds the time of the last real transition.
  The stall age is therefore honest.
- It already treats "between stages" (frontier terminal, next stage not
  started) as not active, which matches today's behaviour for a timeline whose
  last event is terminal.
- `needs_you.py` already combines `is_review` with `compute_liveness`, so the
  anomaly feed and the needs-you count use one definition of liveness.

`is_review` (from `cfactory.status_taxonomy`) excludes `human_review` and the
other review-gate statuses. This fixes fault 2. `compute_liveness` counts review
as active because it is not terminal, so the exclusion has to live here.

The 24h threshold is unchanged: `stale_seconds` (default `_DEFAULT_STALE_SECONDS
= 86_400`) is passed as `deadline_seconds`, so the anomaly feed does not pick
up the cockpit's 15-minute stall deadline (`CFACTORY_STALL_DEADLINE_SECONDS`).

No change to the response shape: `kind`, `severity`, `correlation_key`,
`title` and `detail` are the same fields. The `detail` text keeps its format;
only the values now come from the current state. Consumers stay compatible:
`GET /api/anomalies` (`routes_workitems.py`), the MCP tool (`mcp.py`), the
copilot summary (`anomalies_summary_line`, `copilot/service.py`) and the
frontend schema (`api.ts` `AnomalySchema`).

## Alternatives rejected

- **Make the poll append a timeline event on every status change.** It would
  also fix the timeline, but it changes what the timeline means (events received
  vs. polled observations), touches the store write path that the #105 and #257
  fixes were careful about, and grows every row's JSON timeline. It can be done
  later if the timeline itself needs to be right.
- **Keep review items as a new `awaiting_human` anomaly kind.** It duplicates
  needs-you, and a new `kind` value is a contract change for the frontend
  schema and any agent that switches on `kind`.
- **Compare the timeline's last status with the slice status and skip on a
  mismatch.** It patches the symptom and still measures age from the stale
  event.

## Risks

- **Items with timeline events but no slice status stop being flagged.** Every
  write path sets the slice as well as the timeline, so this should not happen.
  If it does, the item is dropped rather than wrongly flagged, which fits the
  "honest empty state" rule.
- **Age now comes from CFactory's write time, not the upstream event time.**
  `updated_at` is stamped with `_now()` when CFactory stores a change. For
  events that arrive late, the age starts from when CFactory learned about the
  change. That is the same clock the cockpit's stall pill already uses.
- **Existing tests assume a fixed clock.** `tests/test_anomalies.py` passes
  `now=2026-06-05`, while `updated_at` is stamped with the real wall clock. Once
  the check uses `compute_liveness`, the age comes out negative and
  `test_stuck_when_stale_and_not_terminal` fails unless the store's `_now` is
  controlled in the test.
- Prod host: the only runtime change is to what `GET /api/anomalies` returns.
  There are no writes, no migration and no upstream calls.

## Verification

- Unit tests in `tests/test_anomalies.py`, with the store clock controlled
  (monkeypatch `cfactory.store._now`) so `updated_at` lands at a known time:
  - stale `coding` is flagged (existing, adapted);
  - fresh, or terminal-OK, is not flagged (existing, adapted);
  - **regression for #454:** timeline last event `human_review`, then a polled
    slice update to `done` (`WorkItemStore.upsert_snapshot`),
    old: not flagged;
  - stale `human_review` slice: not flagged as stuck, and
    `needs_you.needs_human` is still True for it;
  - `failure` and `handback_loop` tests unchanged and green.
- `PYTHONPATH=apps/backend pytest -v` green. The per-file ratchet (ruff and
  `mypy --strict` over `copilot/anomalies.py` and `tests/test_anomalies.py`)
  gains no violations.
- Runtime check on prod after promotion to `main`: `cfactory_get_anomalies`
  returns no `stuck` entry whose AIFactory `task_list` status is `done` or
  `human_review`. Today that covers the 4 remaining profile tasks
  (009, 011, 014, 018), which are merged and sitting in `human_review`.

## Out of scope

- CFactory's `approve_review` fails with 409 when a PR already exists for the
  branch, and never reaches the merge step (seen 2026-09-23 on task 018). That
  is a separate bug in `actions.py` / AIFactory `create-pr` and gets its own
  issue.

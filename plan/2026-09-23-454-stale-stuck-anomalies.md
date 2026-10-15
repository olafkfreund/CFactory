---
status: approved
issue: 454
spec: spec/2026-09-23-454-stale-stuck-anomalies.md
---

# Plan: stuck anomalies must reflect current upstream status

Self-contained summary of the approved decisions:

- Fix the detector only. The poll keeps writing stage slices without timeline
  events.
- The "stuck" check in `apps/backend/cfactory/copilot/anomalies.py`
  (`_detect_for_item`, section 3) reads the item's current state via
  `cfactory.store.compute_liveness(wi, now=now, deadline_seconds=stale_seconds)`
  instead of `wi.timeline[-1]`.
- An item is `stuck` when `liveness.stalled` is True and
  `is_review(liveness.active_status)` is False. Review-waiting items leave the
  anomaly feed; `needs_you.needs_human` still counts them.
- The threshold stays 24h (`_DEFAULT_STALE_SECONDS`). The response shape and
  the `detail` text format stay the same:
  `no progress for ~{hours}h (last: {service}={status})`.
- The `failure` and `handback_loop` checks are unchanged.
- Out of scope: the `approve_review` 409 when a PR already exists (separate
  issue).

## Steps

1. `tests/test_anomalies.py`: make the fixtures control the store clock.
   `_ev` patches `cfactory.store._now` to return `when` around
   `store.upsert_from_event` (via `pytest.MonkeyPatch.context()`, so callers
   keep their signature), so `updated_at` lands at the event's time. Add a
   matching `_snap(store, key, service, status, when)` helper that calls
   `store.upsert_snapshot(key, service, ServiceState(task_id="t", status=status))`
   under the same patch.
   → verify by running `pytest tests/test_anomalies.py` against the current
   detector: all existing tests still green (the clock patch changes nothing
   for the timeline-based check).

2. `tests/test_anomalies.py`: add the failing tests first.
   - `test_not_stuck_when_polled_slice_is_done` (the #454 regression): `_ev`
     `aifactory=human_review` at OLD, then `_snap` `aifactory=done` at OLD;
     no `stuck`.
   - `test_review_is_not_stuck_but_needs_you`: `_ev` `aifactory=human_review`
     at OLD; no `stuck`, and `needs_human(store.get("1"))` is True.
   → verify by running them against the current detector: both fail, which
   proves they catch the bug.

3. `apps/backend/cfactory/copilot/anomalies.py`: replace section 3 with the
   `compute_liveness` + `is_review` check (code block in the spec). Import
   `compute_liveness` from `cfactory.store` and `is_review` from
   `cfactory.status_taxonomy`. Update the module docstring line about the stale
   check, and drop the `_is_terminal_ok` import if nothing else uses it.
   → verify by running `pytest tests/test_anomalies.py`: all green, including
   the two new tests.

4. Full gates.
   → verify by running `PYTHONPATH=apps/backend pytest -v` (green),
   `ruff check` and `mypy --strict` on the two changed files (no new
   violations against `origin/dev`), and `git diff origin/dev --name-only`
   (only the two code files plus intent/spec/plan).

5. Docs: add a note to the anomalies section of `guides/` or `techdocs/`
   (wherever `get_anomalies` is documented; grep first) saying that `stuck`
   means an active stage with no status change for 24h, and that review-waiting
   items appear under needs-you, not anomalies.
   → verify by grep: the documented meaning matches the code.

6. PR to `dev` linking the intent, spec and plan, with `Closes #454`.
   → verify by CI: `Backend pytest` and `Frontend typecheck + build` green,
   and the ratchet green.

## Tests

```bash
PYTHONPATH=apps/backend pytest tests/test_anomalies.py -v   # new + adapted, all pass
PYTHONPATH=apps/backend pytest -v                           # whole suite green
ruff check apps/backend/cfactory/copilot/anomalies.py tests/test_anomalies.py
mypy --strict apps/backend/cfactory/copilot/anomalies.py
```

After promotion to `main`: `cfactory_get_anomalies` shows no `stuck` entry for
a task whose AIFactory status is `done` or `human_review` (today: tasks 009,
011, 014, 018 must not appear).

## Rollback

Revert the implementation commit. Nothing else changes: no migration, no
config, no upstream writes. The anomaly feed goes back to timeline-based
detection.

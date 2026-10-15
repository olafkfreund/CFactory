---
status: approved
issue: 458
author: olafkfreund
---

# Intent: queued work is not stuck work, and a discard is not a failure

Follow-up to #454 (`intent/2026-09-23-454-stale-stuck-anomalies.md`).

## Problem

Since #455 shipped (prod image `sha-1429f13`, 2026-09-23), the anomaly feed
flags items that were never started as `stuck`. Right after deploy, prod showed
five: `002-create-profile-requirements-acceptance-criteria` and four
`parr-regression-probe` items, all `pfactory=backlog`, idle 267-377h.

The #454 fix judges stuck on the current stage status via `compute_liveness`,
which treats every non-terminal status as active. `backlog`, `pending` and
`queued` (the `is_queued` set) are non-terminal, so a card waiting in the
backlog for over 24h is reported as a hung task. The old detector never
flagged these only because they had no timeline events. The #454 spec's risk
list missed this case.

Second case, added 2026-09-23 (approved scope widening): every deploy and
nightly run of `Factory/scripts/parr_regression.py` creates a PFactory probe
plan session and closes it on teardown with `/discard`. CFactory's shared
vocabulary lists `discarded` as a failed status, so each run shows a red
`failure` / `high` card ("plan stage status='discarded'") until the probe item
is pruned. A discard is a deliberate close; PFactory itself maps `discarded`
to `done` (`PFactory/apps/backend/plan/service_helpers.py`).

A queued item has no agent attached. Nothing is hung, so no recovery is
possible or wanted. Reporting it as stuck overclaims trouble, which is the
"honest empty state" rule #454 set out to restore.

## Proposed outcome

- A stage whose current status is queued (`is_queued`: backlog, pending,
  queued, ...) is never reported as `stuck`.
- `stuck` means an agent-attached stage whose status has not changed for 24h,
  as `techdocs/api.md` already says ("active ... not done, failed or parked
  for review"). The wording gains "or not yet started".
- A stage whose current status is a discard is never reported as a
  `failure`. Other failed statuses (failed, rejected, aborted, ...) still are.
- On today's prod data, the anomaly feed shows 0 stuck items, and a
  post-deploy regression run leaves no failure card.
- After the fix ships: the four leftover `backlog` probe sessions
  (005, 006, 008, 009) and the `002-create-profile-requirements-acceptance-criteria`
  card are discarded in PFactory, leaving the portal clean.

## Affected users and systems

- CFactory backend: `copilot/anomalies.py`, `GET /api/anomalies`, the
  `cfactory_get_anomalies` MCP tool, and the copilot summary line.
- Operators and agents polling anomalies (e.g. `parr-run`).
- Not affected: the upstream factories. Read-only, no writes.

## Constraints

- No new write path to an upstream. (The cleanup discards are a one-off
  operator action, not code.)
- `_contracts/factory_contracts` is vendored byte-exact from the hub (drift
  gate). The shared status vocabulary must not be edited here; the change
  lives in CFactory's own anomaly code.
- Response shape and the existing `failure` / `handback_loop` kinds unchanged.
- Per-file ratchet on `anomalies.py` and `tests/test_anomalies.py` must stay
  clean. Both are clean after #455.

## Open questions

Resolved 2026-09-23: option (a), nowhere new.

1. Should a queued item that has sat for a long time (e.g. 7+ days) surface
   anywhere?
   - (a) Nowhere new: the backlog column and card list already show it.
     **Recommended:** smallest change, and backlog age is a planning concern,
     not a pipeline fault.
   - (b) A new low-severity `idle` anomaly kind. This is a contract change for
     the frontend schema and any agent that switches on `kind`.
   - (c) Count it in needs-you. That muddies "needs a click", since nobody has
     to act on a backlog card right now.

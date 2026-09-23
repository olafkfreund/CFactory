---
status: approved
issue: 454
author: olafkfreund
---

# Intent: stuck anomalies must reflect current upstream status

## Problem

The cockpit's anomaly feed reports work as "stuck" when it is not. On
2026-09-23 it listed 19 stuck items for project 888c19de, while AIFactory
reported zero running tasks: 4 were `done` and 15 were in `human_review`.

Two separate faults produce this:

1. The stuck check in `apps/backend/cfactory/copilot/anomalies.py`
   (`_detect_for_item`) looks only at the last timeline event. The AIFactory
   poll updates the stage's current status without adding a timeline event, so
   a task that finished upstream keeps the age and status of its last event.
   Example: task `001` has current status `done` (updated 2026-09-18), but its
   last event is `human_review` from 2026-09-08, reported as "no progress for
   ~351h".
2. `human_review` is flagged as `stuck`. A task waiting on a human is not hung;
   it needs a click, not a recovery.

The result breaks the "honest empty state" rule: the feed overclaims trouble,
and an operator who trusts it could run `task_recover` on finished or
awaiting-review work, which restarts agents and discards the result.

## Proposed outcome

- A task whose current stage status is terminal-OK (`done` and equivalents) is
  never reported as stuck, whatever its timeline says.
- A task waiting on a human is reported as awaiting review, not as stuck, so
  the operator can tell "click needed" apart from "something is hung".
- With today's data, the feed for project 888c19de shows 0 stuck items and 15
  awaiting review.

## Affected users and systems

- CFactory backend: anomaly detection, `GET /api/anomalies`, the
  `cfactory_get_anomalies` MCP tool, and the copilot's anomaly summary line.
- Cockpit views that render anomalies.
- Operators and agents (e.g. the `parr-run` skill) that poll anomalies to decide
  when to intervene.
- Not affected: the upstream factories. CFactory stays read-only here.

## Constraints

- Must not add any write path to an upstream (human-in-the-loop rule).
- Must not break existing anomaly kinds (`failure`, `handback_loop`) or the
  response shape existing clients parse.
- Per-file ratchet: `anomalies.py` must not gain ruff or `mypy --strict`
  violations.

## Open questions

1. Should the poll also append a timeline event when it sees a status change
   (fixes the timeline itself, larger change), or is it enough for the detector
   to read the current stage status (smaller, fixes only the anomaly feed)?
2. Awaiting-review items: a new anomaly kind (e.g. `awaiting_human`, low
   severity, still listed) or dropped from anomalies entirely because the
   existing needs-you view (`needs_you.py`) already covers them?

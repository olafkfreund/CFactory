---
status: approved
issue: 478
author: olafkfreund
---

# Intent: a board-dispatched card settles when AIFactory finishes CODE

## Problem

A card dispatched from the planning board to AIFactory (the CODE stage) never
settles. It stays `in_progress` with CODE `dispatched`, a `run` sequence never
advances, and TEST is refused because code is not `done`.

The card's join key is written once from the first stage's intake response
(`apps/backend/cfactory/card_intake.py:498-512`, `:553-558`). AIFactory's
`POST /api/tasks/from-issue` returns `task_id = "<project>:<spec>"`, `spec_id`
and `issue_number`, but no `correlation_key`, so the card is joined on
`<project>:<spec>`. AIFactory keys its completion event on the issue number, or
on `af-<spec_id>` when there is none (AIFactory
`apps/web-server/server/services/completion.py`, `correlation_key()`).
`apply_status` (`card_intake.py:815+`) looks the card up by exact key
(`cards.py:813`), finds nothing, and silently does nothing.

Found by the Factory#971 terminal-status audit.

## Proposed outcome

When AIFactory's completion event for a board-dispatched CODE stage arrives
(success or failure), the card's CODE run settles, the card status follows, and
a `run` sequence advances to TEST.

## Affected users and systems

CFactory backend (`card_intake.py`), the planning board (`PlanningBoard.tsx`
via `GET /api/cards`). No AIFactory change.

## Constraints

- Must not change the join key for PFactory or TFactory dispatches.
- Write-once correlation key semantics stay as they are.
- No change to the vendored `_contracts`.

## Open questions

None. A card whose FIRST stage is PLAN (PFactory) keeps PFactory's key; whether
its later AIFactory event joins is a separate question and out of scope here.

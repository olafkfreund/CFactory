---
status: approved
issue: 467
author: olafkfreund
---

# Intent: say "nothing to act on", not "no work item", and document the real action kinds

## Problem

1. **A wrong reason on the propose step.** `POST /api/actions/propose`
   (`apps/backend/cfactory/routes_actions.py:39`) and the MCP propose path
   (`apps/backend/cfactory/mcp.py:1062`, `1069`) turn any `None` from
   `actions.propose()` into 404 "no work item for <key>". But the `propose_*`
   functions in `actions.py` also return `None` when the item exists and has no
   stage the action applies to. Examples: `approve_review` on a task that is
   already `done`, or `delete_task` on a plan-stage item (PFactory has no
   delete). The operator is told the item does not exist when it plainly does.
   Seen on 2026-09-23 with `021-e2e-457-approve-probe-2` right after it merged.
2. **Docs that describe kinds which don't exist.** `techdocs/api.md` (action
   kinds table, around line 99) and `docs/architecture.md` (around line 75) list
   `approve_gate`, `trigger_handoff` and `kick_handback`. The code has
   `approve_plan`, `approve_review`, `reject_review`, `recover`, `delete_task`
   and `dispatch_card` (`ActionKind` in `actions.py`).

## Proposed outcome

- A propose for a key with no work item still answers 404 "no work item".
- A propose for an existing item with nothing to act on answers with a
  distinct status and a reason that names the item's current state, e.g.
  "nothing to approve: aifactory stage is done". The same distinction applies
  in the MCP tool.
- Both docs list exactly the kinds in `ActionKind`, each with its target
  service and endpoint, taken from `actions.py`.

## Affected users and systems

- CFactory backend: `routes_actions.py`, `mcp.py` (propose), and possibly
  `actions.py` (to tell the two `None` cases apart).
- Cockpit and agents that show or branch on the propose error (the frontend
  shows the `detail` text).
- Docs: `techdocs/api.md`, `docs/architecture.md`.

## Constraints

- No change to what can be proposed or executed; this is messaging only.
  Human-in-the-loop unchanged.
- Existing callers that treat 404 as "no such item" must keep working for
  that case.
- Per-file ratchet on the touched Python files; no emojis in docs.

## Open questions

Resolved 2026-09-23 (approved): 1 = 409; 2 = fix every mention repo-wide.

1. **Status for "exists but nothing to act on":** 409 Conflict (the item's
   state conflicts with the action) or 422? **Recommended:** 409, which
   matches how AIFactory reports refusals since Factory#460.
2. **Scope of the docs fix:** only the two stale lists, or also regenerate any
   other mention of the old names? **Recommended:** grep the repo, including
   `guides/`, and fix every mention.

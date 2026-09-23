---
status: draft
issue: 467
intent: intent/2026-09-23-467-propose-no-target-message.md
---

# Spec: say "nothing to act on", not "no work item", and document the real action kinds

Decisions carried from the approved intent:

- An existing item with nothing to act on answers **409** with a reason; a
  missing item keeps **404**.
- The stale action-kind names are fixed everywhere they appear in the repo.

Correction to the intent: `mcp.py:1062` and `1069` are `get_workitem` and
`get_timeline`, where "no work item" is correct. The MCP server has no
propose tool (`grep propose mcp.py copilot/` finds nothing), so the only
propose surface is `POST /api/actions/propose` and its frontend client.

## Design

### Backend: `routes_actions.py` `propose_action`

Today, `propose()` returning `None` becomes 404 "no work item". Change:

1. After a `None` from `propose`, look up `store.get(req.correlation_key)` in
   the same threadpool call:
   - item missing: 404 "no work item for <key>" (unchanged);
   - item present: 409 with `_nothing_to_act_on(kind, wi)`.
2. `_nothing_to_act_on(kind, wi)` is a small pure function in `actions.py`,
   next to the proposers, so it can be tested without HTTP. It names each
   stage's current state, e.g.
   `"nothing to approve_review for '<key>': aifactory is 'done', no stage is in flight"`,
   or, for `delete_task` on a plan-only item,
   `"... pfactory plan sessions cannot be removed; use reject"`. The wording
   reflects the proposer's own rule (`_review_target` skips terminal stages;
   `_delete_target` refuses pfactory), not a guess.
3. The docstring of `propose_action` is updated (404 vs 409). `actions.propose`
   keeps returning `None` (no signature change), so there are no other
   callers to update.

### Frontend: `apps/frontend-web/src/api.ts` `proposeAction`

Add a 409 branch before the generic error: read `detail` from the JSON body
and throw `new Error(detail)`, so the cockpit shows the reason (the callers in
`TaskActions.tsx` and `CopilotPanel.tsx` already render `error.message`). 404
keeps "no actionable task for this item". The API contract drift gate
(Factory#1005, #445) must stay green; the response schema is unchanged, and
only error handling is added.

### Docs

- `techdocs/api.md` "Action kinds" table (around line 97): replace
  `approve_gate`, `trigger_handoff` and `kick_handback` with the six
  `ActionKind` values. Each row gets its target and endpoint as `actions.py`
  builds them:
  - `approve_plan`: PFactory `POST /api/plan/sessions/{sid}/approve`;
  - `approve_review`: AIFactory `worktree/create-pr` then `worktree/merge`,
    or PFactory session approve on a plan stage;
  - `reject_review`: AIFactory or PFactory reject;
  - `recover`: `/api/tasks/{id}/recover`;
  - `delete_task`: `DELETE /api/tasks/{id}` (not for plan stages);
  - `dispatch_card`: `card_intake`.

  Exact endpoints are copied from the constants at the top of `actions.py`
  during implementation. Also add a line on the 404 vs 409 responses.
- `docs/architecture.md` (around line 75): the tool names become the real
  `propose_*` functions.
- The repo-wide grep for `approve_gate|trigger_handoff|kick_handback` fixes
  every remaining mention, including `guides/` and `docs/_posts`. Exception:
  dated blog posts describe history and are left alone, with a note in the
  plan.

## Alternatives rejected

- **Make `propose` return a reason object instead of `None`.** It changes the
  `PROPOSERS` contract and every proposer. The extra `store.get` in the route
  is one cheap read and keeps the change local.
- **422 instead of 409.** Rejected in the intent.

## Risks

- **A client that treats any non-404 as fatal.** The cockpit is the only
  client and is updated here. External scripts calling propose see 409 where
  they saw 404, which is a clearer error but a different code; noted in the
  PR.
- **Race:** the item is deleted between `propose` and `store.get`. That yields
  404, which is correct at that moment.

## Verification

- `tests/test_actions.py`:
  - existing `test_propose_endpoint_404_for_missing_workitem` unchanged;
  - new: `approve_review` on an item whose aifactory stage is `done` gives 409,
    with the stage state in `detail`;
  - new: `delete_task` on a plan-only item gives 409, with the "use reject"
    reason;
  - new unit tests for `_nothing_to_act_on`.

  Run the new route tests first against the current code: they get 404 and
  fail.
- Frontend: `proposeAction` on 409 throws the server `detail`
  (`api.test.ts`, or the existing client test file), plus
  `npm run typecheck` and `npm run build`, and the contract drift gate.
- `PYTHONPATH=apps/backend pytest -v` green, and the ruff and mypy ratchet on
  the touched files passes.
- The grep for the old names returns only the dated blog posts.

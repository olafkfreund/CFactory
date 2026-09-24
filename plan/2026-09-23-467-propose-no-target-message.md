---
status: draft
issue: 467
spec: spec/2026-09-23-467-propose-no-target-message.md
---

# Plan: say "nothing to act on", not "no work item", and document the real action kinds

Self-contained summary of the approved decisions:

- `POST /api/actions/propose`: a missing work item gives 404 "no work item for
  <key>" (unchanged). An existing item that `propose()` returns `None` for
  gives **409**, with a reason from a new pure helper
  `actions._nothing_to_act_on(kind, wi)`. The reason names each stage's
  current state, using the proposers' own rules (`_review_target` skips
  terminal stages; `_delete_target` refuses PFactory plan sessions).
- `actions.propose` keeps its signature (returns `None`). The route does one
  extra `store.get` in the same threadpool call.
- The MCP server has no propose tool, so no change there.
- Frontend `apps/frontend-web/src/api.ts` `proposeAction`: on 409, throw the
  server `detail`; 404 keeps "no actionable task for this item".
- Docs: every mention of `approve_gate`, `trigger_handoff` and `kick_handback`
  is replaced with the real `ActionKind` values (`approve_plan`,
  `approve_review`, `reject_review`, `recover`, `delete_task`,
  `dispatch_card`) or the real `propose_*` functions. Dated blog posts under
  `docs/_posts` stay as history.

## Steps

1. **Tests first** in `tests/test_actions.py`:
   - `approve_review` on an item whose aifactory slice is `done` gives 409, and
     `detail` names the aifactory status;
   - `delete_task` on a PFactory-only item gives 409, and `detail` says plan
     sessions cannot be removed and to use reject;
   - unit tests for `_nothing_to_act_on` for `approve_review`,
     `reject_review`, `recover`, `approve_plan` and `delete_task`;
   - the existing `test_propose_endpoint_404_for_missing_workitem` is kept
     unchanged.

   → verify by running them on the current code: the two route tests get 404
   and fail, and the helper tests fail on import.

2. **`apps/backend/cfactory/actions.py`**: add `_nothing_to_act_on(kind: str,
   wi: WorkItem) -> str`, typed and pure.
   → verify with the unit tests.

3. **`apps/backend/cfactory/routes_actions.py`** `propose_action`: in one
   `run_in_threadpool` call, run `propose()` and, when it returns `None`,
   `store.get()`. Raise 404 when the item is missing and 409 with the helper's
   message otherwise. Update the docstring.
   → verify with the route tests and the full `tests/test_actions.py`.

4. **Frontend `api.ts`**: add a 409 branch in `proposeAction` that parses
   `{"detail": string}` (a tolerant parse; fall back to "nothing to act on for
   this item") and throws it. Add a test in `apps/frontend-web/src/api.test.ts`
   next to the existing client tests.
   → verify with `npm run typecheck`, `npm test`, `npm run build`, and the
   contract drift check from #445.

5. **Docs**:
   - `techdocs/api.md` (action kinds table): the six kinds, with target and
     endpoint copied from the `actions.py` constants, plus one line on 404 vs
     409;
   - `techdocs/index.md:22`, `techdocs/architecture.md:111`,
     `docs/architecture.md:75`: the real kind names and `propose_*` functions;
   - `techdocs/dependencies.md:44-46`: check how the file is produced first. If
     a script generates it, fix the script's source text and regenerate;
     otherwise edit it by hand.

   → verify by grepping `approve_gate|trigger_handoff|kick_handback` over the
   repo, excluding `node_modules`, `intent/`, `spec/`, `plan/` and
   `docs/_posts`: 0 hits.

6. **Gates**: `PYTHONPATH=apps/backend pytest -v`, with the venv's `bin` first
   on `PATH` (the ratchet test needs the venv mypy);
   `scripts/ratchet_lint.py --base origin/dev` for ruff and mypy; the frontend
   typecheck and build.
   → verify all green.

7. **PR** to `dev` with `Closes #467`, linking the intent, spec and plan. Note
   the 404 to 409 change for external callers in the PR description. Merge
   when green, then promote to `main` via a `sync/dev-to-main-467` PR and watch
   the deploy.
   → verify: after deploy, proposing `approve_review` on a `done` item in prod
   returns 409 with a reason.

## Tests

```bash
PATH=$PWD/apps/backend/.venv/bin:$PATH PYTHONPATH=apps/backend python -m pytest tests/test_actions.py -v
PATH=$PWD/apps/backend/.venv/bin:$PATH PYTHONPATH=apps/backend python -m pytest -q
cd apps/frontend-web && npm run typecheck && npm test -- --run && npm run build
```

## Rollback

Revert the implementation commit and promote again. There is no data or
config change. Callers get 404 again for both cases.

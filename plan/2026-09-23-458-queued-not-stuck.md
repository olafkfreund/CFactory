---
status: draft
issue: 458
spec: spec/2026-09-23-458-queued-not-stuck.md
---

# Plan: queued work is not stuck work, and a discard is not a failure

Self-contained summary of the approved decisions:

- In `apps/backend/cfactory/copilot/anomalies.py`, `_detect_for_item`:
  - Section 3 (stuck): flag only when `liveness.stalled and not
    is_review(status) and not is_queued(status)`, where `status =
    liveness.active_status`. `compute_liveness` itself is unchanged.
  - Section 1 (failure): skip when the stage status, stripped and lowercased,
    is in `_DISCARDED = frozenset({"discard", "discarded"})`. All other failed
    statuses are still reported.
- The vendored `_contracts/factory_contracts` vocabulary is NOT edited (drift
  gate).
- Long-idle queued items surface nowhere new (option a). Accepted trade-off: a
  downstream stage stuck in `pending` is no longer flagged.
- Not in scope: `cancelled` / `aborted`, and `task_process.py` `_FAIL_WORDS`.
- Docs: `techdocs/api.md` states both rules.
- After deploy only: discard the PFactory plan sessions for `005`, `006`, `008`,
  `009-parr-regression-probe` and `002-create-profile-requirements-acceptance-criteria`.

## Steps

1. `tests/test_anomalies.py`: add four tests using the existing `_ev` / `_snap`
   clock helpers:
   - `test_queued_backlog_is_not_stuck`: `_snap` `pfactory=backlog` at OLD;
     no `stuck`.
   - `test_downstream_pending_is_not_stuck`: `_snap` `aifactory=pending` at
     OLD; no `stuck`.
   - `test_discarded_is_not_a_failure`: `_ev` `pfactory=discarded` at FRESH;
     no `failure`.
   - `test_rejected_is_still_a_failure`: `_ev` `pfactory=rejected` at FRESH;
     `failure` present.
   → verify by running `pytest tests/test_anomalies.py` against the unchanged
   detector: the backlog, pending and discarded tests fail, and the rejected
   test passes.

2. `apps/backend/cfactory/copilot/anomalies.py`: apply both changes. Import
   `is_queued` from `cfactory.status_taxonomy`, add `_DISCARDED` with a
   one-line reason comment, and update the module docstring sentence about
   stuck to also say "not yet started".
   → verify by running `pytest tests/test_anomalies.py`: all green.

3. Gates.
   → verify by running `PATH=apps/backend/.venv/bin:$PATH PYTHONPATH=apps/backend
   pytest -q` (all green), then commit and run `scripts/ratchet_lint.py --base
   origin/dev --tool ruff|mypy` (PASSED). `git diff origin/dev --name-only`
   should list only `anomalies.py`, `test_anomalies.py`, `techdocs/api.md` and
   the three #458 artifacts.

4. `techdocs/api.md`: the `failure` bullet gains "a discard is a deliberate
   close and is not reported". In the `stuck` bullet, "active (not done, failed
   or parked for review)" becomes "active (not done, failed, parked for review,
   or not yet started)".
   → verify by `grep -c "deliberate close\|not yet started" techdocs/api.md`
   returning 2 (the old sentence wraps across lines, so grep the new phrases).

5. PR to `dev` (`Closes #458`, linking the intent, spec and plan). When CI is
   green, merge. Then open a `sync/dev-to-main-458` PR to `main`, merge it when
   green, and watch `deploy.yml` until it completes successfully.
   → verify by `kubectl -n factory get deploy cfactory` showing the new
   `sha-<main>` image and the rollout complete; `cfactory_get_anomalies` has no
   `stuck` for backlog items and no `failure` for the post-deploy probe.

6. Cleanup, only after step 5 is verified. Port-forward `svc/pfactory 3114`.
   Authenticate with the pfactory MCP server's configured bearer, read without
   printing it; if it is rejected, stop and ask. For each of the five
   correlation keys, resolve the plan session id with the PFactory MCP
   `plan_list`, and check the title and `backlog` status match before acting.
   Then `POST /api/plan/sessions/{sid}/discard` with
   `{"actor": "cockpit-operator", "reason": "#458 cleanup"}`, one at a time,
   stopping on the first non-2xx.
   → verify by `cfactory_get_anomalies` returning `count: 0` after the next
   poll, and the portal's anomalies panel being empty.

## Tests

```bash
PYTHONPATH=apps/backend apps/backend/.venv/bin/python -m pytest tests/test_anomalies.py -v
PATH=$PWD/apps/backend/.venv/bin:$PATH PYTHONPATH=apps/backend python -m pytest -q
python scripts/ratchet_lint.py --base origin/dev --tool ruff
python scripts/ratchet_lint.py --base origin/dev --tool mypy
```

## Rollback

- Code: revert the implementation commit on `dev`, then promote again. No
  migration or config change.
- Cleanup: discards cannot be undone; PFactory has no un-discard. They are
  limited to the five named sessions, which are test probes plus one card you
  approved for discarding.

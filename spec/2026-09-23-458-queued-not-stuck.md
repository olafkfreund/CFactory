---
status: draft
issue: 458
intent: intent/2026-09-23-458-queued-not-stuck.md
---

# Spec: queued work is not stuck work, and a discard is not a failure

Decisions carried from the approved intent:

- Queued stages (`is_queued`: backlog, draft, icebox, pending, queued, ...)
  are never `stuck`. Long-idle queued items surface nowhere new (option a).
- A discarded stage never raises a `failure` anomaly. All other failed
  statuses still do.
- The vendored `_contracts/factory_contracts` vocabulary is not edited (drift
  gate). The change lives in CFactory's own `copilot/anomalies.py`.
- After the fix is live, the leftover backlog probe sessions and the `002`
  profile card are discarded in PFactory.

## Design

Two changes, both in `apps/backend/cfactory/copilot/anomalies.py`, inside
`_detect_for_item`.

**Section 3 (stuck):** add `is_queued` next to the `is_review` exclusion that
#455 introduced:

```python
liveness = compute_liveness(wi, now=now, deadline_seconds=stale_seconds)
status = liveness.active_status
if liveness.stalled and not is_review(status) and not is_queued(status):
    ...
```

`compute_liveness` stays as it is. It feeds the cockpit's stall pill and
`needs_you`, and changing what "active" means there would ripple into both.
`is_queued` comes from `cfactory.status_taxonomy`, which re-exports it from
the contracts module.

**Section 1 (failure):** skip a stage whose status is a discard:

```python
# A discard is a deliberate close (a human, or the parr-regression probe
# teardown), not a fault. PFactory maps it to done (#458). The shared
# vocabulary keeps it under "failed", so the exemption lives here.
_DISCARDED = frozenset({"discard", "discarded"})

if _is_failure(s.status) and (s.status or "").strip().lower() not in _DISCARDED:
```

It is an exact match on the status string, which is what PFactory emits
(`TERMINAL_STATUSES` in `PFactory/apps/backend/plan/completion.py`). Token
matching (e.g. `plan_discarded`) is not needed: no upstream emits such a
status today.

A discarded stage is terminal (`is_terminal` is True), so it also cannot be
`stuck`. Nothing else is needed for it.

**Docs:** in `techdocs/api.md`, the anomaly section that #455 added gains two
statements:

- `failure`: "a discard is a deliberate close and is not reported".
- `stuck`: "active" now also excludes "not yet started (backlog, pending,
  queued)".

**Operational cleanup, after the fix is on `main`** (not code): discard the
PFactory plan sessions for `005`, `006`, `008` and `009-parr-regression-probe`
and for `002-create-profile-requirements-acceptance-criteria`. Resolve each
session id with the PFactory MCP `plan_list` / `plan_get`, then call
`POST /api/plan/sessions/{sid}/discard` with
`{"actor": "cockpit-operator", "reason": "#458 cleanup"}` (the same call the
regression teardown uses). CFactory has no discard action, and PFactory has no
plan-session DELETE, so discard is the only close. It has to happen after the
deploy: before the fix, each discard would itself raise a `failure` card.

## Alternatives rejected

- **Change `compute_liveness` so that queued is not active.** It would also
  change the cockpit stall pill and `needs_you`, which is a wider blast radius
  than this bug.
- **Remove `discarded` from `_FAILED_TOKENS`.** The vocabulary is vendored from
  the hub and shared by every factory. It would need a hub change and a
  re-vendor, and it would also change `is_terminal` semantics for the other
  services. It may be worth proposing upstream separately.
- **Also exempt `cancelled` / `aborted`.** No evidence they are used as
  deliberate closes today, and an abort can be a real fault. Left out (YAGNI).
- **Make the regression probe hard-delete instead of discard.** PFactory has
  no plan-session DELETE endpoint.

## Risks

- **A downstream stage that stays `pending` for over 24h is no longer
  flagged.** Example: AIFactory never picks up a task after the plan was
  approved. This is a hand-off stall. Under #455 it would show as `stuck`; after
  this change it will not. It is accepted under option (a) of the approved
  intent. A hand-off watchdog belongs in its own issue if it turns out to be
  needed.
- **Other surfaces still paint discard as failure.** `task_process.py`
  `_FAIL_WORDS` includes "discard", so the process view is unchanged. This is
  out of scope; the intent covers the anomaly feed only.
- **Cleanup needs PFactory API write access** from the operator's machine
  (port-forward and token), and it is irreversible. It touches exactly the five
  named sessions and nothing else.

## Verification

- `tests/test_anomalies.py` (using the clock helpers from #455):
  - stale `pfactory=backlog` snapshot: not `stuck` (the #458 regression);
  - stale `aifactory=pending` snapshot: not `stuck`;
  - `pfactory=discarded` event: not `failure`;
  - `pfactory=rejected` event: still `failure` (guards the exemption's width);
  - all existing tests green.
  Run first against the current code: the backlog and discarded tests fail.
- `PYTHONPATH=apps/backend pytest -v` green, and the ruff and mypy ratchet
  passes against `origin/dev`.
- Prod, after promotion and deploy:
  - the post-deploy `parr_regression` run completes and `cfactory_get_anomalies`
    shows no `failure` for its probe;
  - after the cleanup discards, `cfactory_get_anomalies` returns `count: 0`.

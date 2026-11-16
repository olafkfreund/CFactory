---
status: approved
issue: 517
spec: spec/2026-11-16-517-review-fixes-cfactory.md
coders:
  1: coder   # small, I implement directly (below the handoff threshold)
reviewers:
  - codex    # independent read of the diff
---

# Plan: reconcile across all orgs

Approved decisions: poll every extra org key via a sibling adapter; reconcile only on a full
view; skip on any extra-org failure; no behaviour change without extra keys.

## Steps

1. `apps/backend/cfactory/adapters/base.py`, `adapters/__init__.py`, `progress.py`,
   `routes_events.py`: add `with_token`, `live_ids_all_orgs`, replace both guards
   → verify by `pytest tests/test_reconcile_extra_orgs.py`. Traps: none.
2. `tests/test_reconcile_extra_orgs.py`, `tests/test_upstream_ws.py` (old skip-reconcile test rewritten): three regression tests → full `pytest`, `ruff check`.

## Tests

`PYTHONPATH=apps/backend pytest` green; new tests fail on the old guard.

## Rollback

Revert the commit.

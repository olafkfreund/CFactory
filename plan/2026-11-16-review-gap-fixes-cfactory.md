---
status: draft
issue: 518
spec: spec/2026-11-16-review-gap-fixes-cfactory.md
coders:
  1: coder   # small, implemented directly by the lead
reviewers:
  - codex
---

# Plan: log when extra-org reconcile is skipped

Approved decisions: warn in `live_ids_all_orgs` with service name and exception class only;
keep returning `None`; document the pause in the `ORG_TOKENS` env row.

## Steps

1. `apps/backend/cfactory/adapters/__init__.py`: add `logger`, log in the `except AdapterError`
   branch. `docs/dev/environment-reference.md:43`: add the pause sentence.
   -> `PYTHONPATH=apps/backend apps/backend/.venv/bin/pytest tests/test_reconcile_extra_orgs.py`
   Traps: never log the token or error text.
2. `tests/test_reconcile_extra_orgs.py`: add `test_poll_logs_warning_when_reconcile_skipped`
   -> fails on origin/dev code, passes with step 1.

## Tests

`tests/test_reconcile_extra_orgs.py` (4 pass); full `pytest` in CI.

## Rollback

Revert the commit.

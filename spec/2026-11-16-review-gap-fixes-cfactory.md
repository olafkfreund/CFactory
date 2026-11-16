---
status: draft
issue: 518
intent: intent/2026-11-16-review-gap-fixes-cfactory.md
---

# Spec: log when extra-org reconcile is skipped

## Design

In `live_ids_all_orgs` (`apps/backend/cfactory/adapters/__init__.py`) the `except AdapterError`
branch logs `logger.warning("reconcile skipped for <service>: an extra org key could not be
listed (<ExceptionClass>)")` and still returns `None`. Both callers (`progress.py`,
`routes_events.py`) go through this helper, so one log site covers poll and refresh. Only the
service value and the exception class are logged: no token, no upstream text. The `ORG_TOKENS`
row in `docs/dev/environment-reference.md` gains the pause-while-failing sentence.

## Alternatives rejected

- Logging in each caller: two sites, and the helper alone knows an extra org failed.
- Reconciling the orgs that did list: a partial view could clear a failed org's live stages.
- Surfacing an error field in `/api/refresh`: an API change, larger than a focused fix.

## Risks

One warning per poll cycle while a key is dead (log noise, intended).

## Verification

New test in `tests/test_reconcile_extra_orgs.py` asserts the warning (service named, token
absent); it fails on the unfixed code. `pytest tests/test_reconcile_extra_orgs.py` passes.

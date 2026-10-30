---
status: draft
issue: 486
intent: intent/2026-10-30-486-remove-legacy-correlation-keys.md
---

# Spec: Remove the legacy bare-number correlation-key paths

## Design

Delete the four window paths #486 lists, nothing else:

1. `CardStore.get_by_correlation_key` (`apps/backend/cfactory/cards.py`): drop
   the bare-number fallback block; return the exact-key match only. Remove
   `_QUALIFIED_KEY` and the `parse_repo_ref` import from `cards.py` if
   nothing else uses them (`_issue_project` stays: it has other callers).
2. `apps/backend/cfactory/store.py`: delete `_adopt_legacy_row` and its calls
   in `upsert_from_event` and `upsert_snapshot`; drop `_QUALIFIED_KEY` if
   unused.
3. `_upstream_key` (`card_intake.py`): AIFactory with no `correlation_key`
   and a `spec_id` returns `af-<spec_id>` only; the bare issue-number branch
   goes. AIFactory's from-issue now answers with `correlation_key`
   (AIFactory `routes/from_issue.py`), so the generic loop takes it.
4. The three adapters: `correlation_key` is the upstream's
   `correlation_key`/`correlationKey`, else `task_id`. Drop the issue-number
   fallback and the now-unused `issue` lookup where nothing else reads it.

Tests: delete the tests that exist only to cover the window
(`tests/test_correlation_key_scope.py`: bare-row/card adoption, adoption
stall clock, slice move, card fallback repo match, late old-format event,
provider-qualified card fallback). Replace them with one regression test that a
qualified key no longer adopts a bare row or card (both stay where they are).
Update expectations that assumed an issue number becomes the key
(`tests/test_adapters.py`, `tests/test_card_stage_actions.py`).

## Alternatives rejected

- Keep the fallbacks longer: the exit condition is met; keeping them keeps the
  cross-repo collision alive for any producer that regresses.
- Rewrite remaining bare-keyed rows: #486 says leave them as history.

## Risks

- A producer that still sends a bare number (AIFactory's `correlation_key()`
  returns `str(issue)` when it has no repo) gets a row on that bare key with
  no migration. Same as a fresh key: visible, not lost.
- Polled rows with an issue but no `correlation_key` now key on the task id,
  so a poll-only join by issue number between services stops. The event path
  carries the scoped key and is authoritative.
- Host: the k3d CFactory deployment only, after a normal image roll.

## Verification

- `pytest tests/` in CFactory green; ruff/format clean.
- `git grep -n "legacy (window\|_adopt_legacy_row\|legacy correlation key migrated" -- apps`
  returns nothing.
- New regression test fails on origin/dev (adoption happens) and passes after.

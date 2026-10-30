---
status: approved
issue: 486
spec: spec/2026-10-30-486-remove-legacy-correlation-keys.md
---

# Plan: Remove the legacy bare-number correlation-key paths

Approved decisions: delete the four #484 window paths and only them. Keys come
from the producer's `correlation_key`, else the task id / `af-<spec_id>`
synthetic key. Existing bare-keyed rows and cards are left untouched (no data
migration). Window-only tests are deleted; one regression test proves a
qualified key no longer adopts a bare row or card.

Test command (from repo root): `PYTHONPATH=apps/backend apps/backend/.venv/bin/pytest -q <paths>`.
Lint: `apps/backend/.venv/bin/ruff check --config ruff.toml <files>` and
`ruff format --check --config ruff.toml <files>` (use `ruff` on PATH if the
venv has none). Never `--fix` files you did not change.

## Steps

1. `apps/backend/cfactory/store.py`: delete `_adopt_legacy_row` (~l.744-790)
   and the two `row = self._adopt_legacy_row(...)` calls in
   `upsert_from_event` (~l.489) and `upsert_snapshot` (~l.552). Delete
   `_QUALIFIED_KEY` (l.33) and any import left unused (`re`?).
   → verify: `git grep -n "_adopt_legacy_row\|_QUALIFIED_KEY" apps/backend/cfactory/store.py` empty; ruff clean.
   Traps: keep the IntegrityError retry loop intact.
2. `apps/backend/cfactory/cards.py` `get_by_correlation_key` (~l.818-858):
   remove the `if row is None and (m := _QUALIFIED_KEY.match(...))` block; keep
   the exact-match query and return. Delete `_QUALIFIED_KEY` (l.99) and the
   `parse_repo_ref` import (l.65) if no longer used. Keep `_issue_project`
   (used at ~l.1431). Trim the docstring if it mentions the fallback.
   → verify: ruff clean on cards.py.
3. `apps/backend/cfactory/card_intake.py` `_upstream_key` (~l.498-520): the
   AIFactory branch becomes `return f"af-{body['spec_id']}"` (no issue
   number); update the docstring: from-issue answers with `correlation_key`,
   which the loop takes; without one the event key is `af-<spec_id>`.
   → verify: ruff clean.
4. `apps/backend/cfactory/adapters/{aifactory,pfactory,tfactory}.py`: the
   `correlation_key=` becomes `str(first(row, "correlation_key", "correlationKey") or task_id)`;
   delete the `# legacy (window, #484 follow-up)` comment and the `issue = first(...)`
   lookup where nothing else in the function reads `issue`.
   → verify: `git grep -n "legacy (window" apps` empty; ruff clean.
5. Tests:
   - `tests/test_correlation_key_scope.py`: delete
     `test_bare_row_and_card_are_adopted_by_the_qualified_key`,
     `test_adoption_keeps_the_stall_clock_and_needs_a_task_id`,
     `test_slice_moves_into_an_existing_qualified_row`,
     `test_card_fallback_only_takes_a_card_from_the_keys_repo`,
     `test_late_old_format_event_does_not_roll_back_the_qualified_slice`,
     `test_card_fallback_matches_a_provider_qualified_key`, and any helper or
     import left unused. Add `test_qualified_key_leaves_bare_rows_and_cards_alone`:
     a bare `"7"` row (snapshot, task t1) and a card keyed `"7"` with
     issue_ref `a/x#7`; an event on `a/x#7` for t1 creates its own row,
     `store.get("7")` still exists, and `cards.get_by_correlation_key("a/x#7")` is None.
   - `tests/test_adapters.py`: rows that relied on the issue number (~l.105-114,
     ~l.170-200 `test_hydrate_threads_services_by_correlation_key`) carry an
     explicit `"correlation_key": "o/r#42"` and assert on it; add one assertion
     that a row with only an issue number keys on its task id.
   - `tests/test_card_stage_actions.py` (~l.188): the `(42, "42")` case becomes a
     from-issue body that carries `"correlation_key": "o/r#42"` and expects that key.
   → verify: new test fails on origin/dev's store.py and passes now; then the full
     suite `PYTHONPATH=apps/backend apps/backend/.venv/bin/pytest -q tests` green.
   Traps: other tests may build bare keys like "42" from explicit
   correlation_key payloads (test_enterprise, test_audit) — those are explicit
   keys, leave them.

## Tests

`PYTHONPATH=apps/backend apps/backend/.venv/bin/pytest -q tests` → all pass.
`git grep -n "legacy (window\|_adopt_legacy_row\|legacy correlation key migrated" -- apps` → empty.

## Rollback

Revert the PR. No data or schema changed, so a revert restores the fallbacks
with nothing to repair.

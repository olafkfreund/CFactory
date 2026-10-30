---
status: draft
issue: 486
author: olafkfreund
---

# Intent: Remove the legacy bare-number correlation-key paths

## Problem

#484 / AIFactory#1775 scoped the correlation key to the repo (`owner/repo#7`).
For a compatibility window CFactory also accepts the old bare number (`7`) and
migrates bare-keyed rows and cards onto the new key. The window ended on
2026-10-29. The fallback code is now dead weight, and the bare-number path is
the collision #484 fixed: issue 7 in two repos lands on one row.

## Proposed outcome

CFactory joins work items and cards only on the key a producer sends, or on
the task id / synthetic key when there is none. No code renames a bare-number
row or card. Rows still keyed on a bare number stay as finished history.

## Affected users and systems

CFactory backend only: `cards.py`, `store.py`, `card_intake.py`,
`adapters/{aifactory,pfactory,tfactory}.py` and their tests. No producer,
schema or gitops change.

## Constraints

- Exit condition from #486, checked read-only on 2026-10-30 against the live
  CFactory store and pod logs: no `work_items` row keyed on a bare number was
  written after 2026-10-17; no row created before the window has a
  repo-scoped key (no migration ever ran); `legacy correlation key migrated`
  appears 0 times in the retained pod logs (2.8 days, pod age). Caveat: the
  store holds no repo-scoped key at all, so no issue-backed run reached
  CFactory during the window. The evidence shows no producer sends bare
  numbers; it cannot show the scoped path working end to end.
- Keep the task-id and `af-<spec_id>` synthetic fallbacks.
- Do not touch or rewrite existing bare-keyed rows.

## Open questions

None.

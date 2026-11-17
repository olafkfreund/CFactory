---
status: approved
issue: 518
author: olafkfreund
---

# Intent: log when extra-org reconcile is skipped

## Problem

`live_ids_all_orgs` returns `None` when any extra org key cannot be listed (revoked or expired
key, 401), and the poll and `/api/refresh` then skip `reconcile_snapshot` for that whole factory.
The skip is deliberate (a partial view must never clear anything), but nothing is logged and the
env reference does not mention it, so ghost running stages stay on the board with no clue why.

## Proposed outcome

A warning naming the service is logged each time reconcile is skipped, and the `ORG_TOKENS` row
documents that reconcile pauses while any extra key fails.

## Affected users and systems

Operators of CFactory with `CFACTORY_<SVC>_ORG_TOKENS`; `adapters/__init__.py`, the env reference.

## Constraints

The skip behaviour stays. Never log tokens or upstream error text.

## Open questions

None. Only #518 is in this round; no finding was bigger than a focused fix.

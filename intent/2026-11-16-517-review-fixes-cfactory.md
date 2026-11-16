---
status: draft
issue: 517
author: olafkfreund
---

# Intent: stale-stage reconcile must work with extra org keys

## Problem

`CFACTORY_<SVC>_ORG_TOKENS` (#517, Factory#3702) opens one extra WS socket per org key. The poll
and `POST /api/refresh` list only the primary org, so reconcile would blank other orgs' running
stages; the code therefore skips reconcile for the whole service
(`progress.py` and `routes_events.py`, `extra_tokens_for` guard). A task in org B whose terminal
WS event is missed then shows "running" forever; nothing clears it.

## Proposed outcome

With extra org keys set, the poll and refresh list every org (primary plus each extra key), and
reconcile clears only stages no org reports. If any org cannot be listed, reconcile is skipped for
that cycle (never clear on a partial view).

## Affected users and systems

CFactory cockpit operators with `*_ORG_TOKENS` set; `progress.py`, `routes_events.py`, adapters.

## Constraints

No change when no extra keys are set. Never clear on a failed listing. No secrets logged.

## Open questions

None. The "primary-org tasks are also affected" part of the report is mostly wrong: finished
primary tasks are still listed and hydrated; only upstream-deleted ones linger.

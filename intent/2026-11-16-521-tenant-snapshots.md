---
status: approved
issue: 521
author: olafkfreund
---

# Intent: tenant-scoped snapshot broadcasts and key-binding migration

## Problem

With multi-tenant on, the poll loop and `POST /api/refresh` broadcast the whole unscoped store to every cockpit client, so one tenant's work items reach another tenant's browser (documented limit of #519). Binding an org key to a tenant (`tenant=key`) after its work items already exist under `default` collides on the globally unique correlation key, and there is no migration. Three docs also state stale facts: the `CFACTORY_AIFACTORY_TOKEN` row says dev-only although #510 is merged; the roadmap and the August-November post say the last commit on `main` is #509.

## Proposed outcome

Snapshot broadcasts reach each client with only its own tenant's items. Single-tenant behaviour is unchanged. A documented script moves chosen `default` work items to a tenant. The docs state dated facts that do not go stale.

## Affected users and systems

CFactory backend (`ws.py`, `progress.py`, `routes_events.py`, `store.py`), operators binding org keys, docs.

## Constraints

No wire-shape change. Multi-tenant off is byte-for-byte as before. The migration never runs implicitly.

## Open questions

None. Decided under owner delegation 2026-11-16: per-client filtering over a tenant field on WorkItem (smaller, no payload change); an explicit script over a startup migration (rows carry no org, so only the operator knows which keys move).

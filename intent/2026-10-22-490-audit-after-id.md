---
status: draft
issue: 490
author: olafkfreund
---

# Intent: /api/audit after_id paging for the SIEM forwarder

## Problem

`AuditStore.list(limit=100)` (apps/backend/cfactory/audit.py) returns only the
newest 100 rows, and `GET /api/audit` (apps/backend/cfactory/routes_actions.py)
takes no parameters. When more than 100 audit rows accumulate between runs of
the factory-gitops `audit-siem-forward` job, the older rows can never be read,
so they never reach the SIEM. factory-gitops#288 made the forwarder alert and
stop on that gap; the gap itself is still there.

## Proposed outcome

A client holding a high-water mark id can call `GET /api/audit?after_id=<id>`
and get the rows after it, oldest first, and keep paging until a short page,
so every row is reachable. A call without `after_id` behaves exactly as today.

## Affected users and systems

- CFactory backend `GET /api/audit` and `AuditStore.list`, openapi.yaml.
- factory-gitops `audit-siem-forward` (the consumer; its loop is a separate PR there).
- The Audit view, which polls `/api/audit` with no params (must be unaffected).

## Constraints

- Default response (no params) unchanged: newest 100, newest first, same keys
  (`count`, `attribution`, `entries`).
- Page size bounded so one request cannot become a full-table read.
- Same `read` scope as today; no new data exposed.

## Open questions

None.

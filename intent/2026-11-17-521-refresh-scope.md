---
status: draft
issue: 521
author: olafkfreund
---

# Intent: scope `/api/refresh` writes per tenant and refresh the stale docs

## Problem

With multi-tenant on, `POST /api/refresh` receives a store already scoped to the caller's tenant (`store_dep`) and hydrates every adapter's deployment-wide result into it. The adapter lists use the primary key, which is not any caller's key, so a caller of tenant B writes the primary org's work items under tenant B and reconciles stages against the same scope. #522 fixed what clients receive; this is the write path it left open. The docs also state stale facts: the `CFACTORY_{AIFACTORY,PFACTORY,TFACTORY}_TOKEN` and `CFACTORY_MODEL_GATEWAY_*` rows say dev-only although #510 and #513 reached `main` in the 2026-11-17 promotion; the post and roadmap name old heads.

## Proposed outcome

In multi-tenant mode a refresh writes primary-key results only to the tenant the poll loop writes them to (`default`), whoever calls it. Single-tenant behaviour is unchanged. Docs state dated facts that match `main`.

## Affected users and systems

CFactory backend `routes_events.py`; operators of multi-tenant deployments; `docs/dev/environment-reference.md`, `docs/roadmap.md`, the August-November post.

## Constraints

No wire-shape change (`{"refreshed": {...}}` and snapshots as today). Multi-tenant off is byte-for-byte as before.

## Open questions

None. Decision (owner delegation 2026-11-17): the refresh target is the `default` tenant, the same one `_poll_loop` uses with the unscoped store; this is the least change and cannot leak across tenants. Binding extra org keys to tenants during refresh stays with the live websocket path (#517) and is not added here.

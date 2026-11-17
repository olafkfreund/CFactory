---
status: draft
issue: 521
author: olafkfreund
---

# Intent: scope `/api/refresh` writes per tenant and refresh the stale docs

## Problem

With multi-tenant on, `POST /api/refresh` receives a store already scoped to the caller's tenant (`store_dep`) and hydrates every adapter's deployment-wide result into it. The adapter lists use the primary key, which is not any caller's key, so a caller of tenant B writes the primary org's work items under tenant B and reconciles stages against the same scope. #522 fixed what clients receive; this is the write path it left open. The docs also state stale facts: the `CFACTORY_{AIFACTORY,PFACTORY,TFACTORY}_TOKEN` and `CFACTORY_MODEL_GATEWAY_*` rows say dev-only although #510 and #513 reached `main` in the 2026-11-17 promotion; the post and roadmap name old heads.

## Proposed outcome

In multi-tenant mode a refresh writes primary-key results exactly as the background poll loop does (unscoped store: existing rows are updated where they sit, new rows get `default`), whoever calls it, so a caller's tenant never receives the deployment-wide results. Single-tenant behaviour is unchanged. Docs state dated facts that match `main`.

## Affected users and systems

CFactory backend `routes_events.py`; operators of multi-tenant deployments; `docs/dev/environment-reference.md`, `docs/roadmap.md`, the August-November post.

## Constraints

No wire-shape change (`{"refreshed": {...}}` and snapshots as today). Multi-tenant off is byte-for-byte as before.

## Open questions

None. Decision (owner delegation 2026-11-17): refresh uses the unscoped view, the same as `_poll_loop`. Codex review rejected the first idea (write to `scoped(default)`): correlation keys are globally unique, so a row the bug already filed under another tenant would make the next refresh fail with `IntegrityError`. The unscoped view updates such rows in place and cannot collide. Limit, not fixed here: this restricts the refresh path to what polling already does; it does not make background polling tenant-aware, and rows already misfiled under a caller's tenant stay there until moved with `scripts/retenant_work_items.py`. Binding extra org keys to tenants stays with the websocket path (#517).

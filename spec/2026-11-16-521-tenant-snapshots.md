---
status: draft
issue: 521
intent: intent/2026-11-16-521-tenant-snapshots.md
---

# Spec: tenant-scoped snapshot broadcasts and key-binding migration

## Design

- `ws.py`: `ConnectionManager.broadcast_snapshot(store, multi_tenant)`. Multi-tenant off: one unscoped `store.list()` to all (as today). On: for each distinct tenant among connected clients, `store.scoped(t).list()` sent only to that tenant's sockets. The DB read runs in a threadpool. `scoped()` copies from any store, so `/api/refresh` passes its injected (possibly already scoped) store and the test seam is kept.
- `progress._poll_loop` and `routes_events.refresh` call it instead of building the snapshot inline.
- `WorkItemStore.retenant(tenant, keys, from_tenant="default") -> int` (unscoped store only): sets `tenant_id` for the given correlation keys whose current tenant is `from_tenant`, returns rows changed; keys owned by another tenant are skipped, never moved. `scripts/retenant_work_items.py --tenant T --keys-file F [--dry-run]` wraps it and reads `CFACTORY_DATABASE_URL`. Rows hold no org, so the operator lists the keys (for example from the cockpit or `GET /api/workitems`).
- Docs: env reference row for `CFACTORY_AIFACTORY_TOKEN` (merged to dev via #510, released to main by the next dev-to-main sync) and the ORG_TOKENS row (snapshots now scoped, migration command); roadmap and post state "as of 2026-11-14, main ended at #516; dev carries newer merges" with a pointer to the live merged-PR list instead of a claimed head.

## Alternatives rejected

- Tenant on WorkItem payloads: changes the wire shape and every consumer, and still needs per-client filtering.
- Startup migration: cannot know which rows belong to which org.

## Risks

Codex-confirmed limit, out of scope: `/api/refresh` still hydrates deployment-wide adapter results into the caller's scoped store (ownership of unseen primary-org keys); this change fixes what clients receive, not that write path. Follow-up.


One list per connected tenant per poll cycle: cost grows with tenants, bounded by connected clients. A wrong key list moves the wrong rows; `--dry-run` shows the count first.

## Verification

Tests: two clients of different tenants receive only their own snapshot from the poll-loop and `/api/refresh` paths; multi-tenant off sends the full list; `retenant` moves listed keys and the scoped store then sees them. Existing suite and ruff pass.

---
status: draft
issue: 521
intent: intent/2026-11-17-521-refresh-scope.md
---

# Spec: scope `/api/refresh` writes per tenant and refresh the stale docs

## Design

- `store.py`: `WorkItemStore.unscoped()` returns a copy of the view with `_tenant = None` (mirror of `scoped()`).
- `routes_events.refresh`: hydrate, `reconcile_snapshot` and the live-id work use `store.unscoped()`, the view `_poll_loop` already uses. The injected `store` (possibly scoped to the caller) is still what `broadcast_snapshot` receives, which re-scopes per connected tenant, so what each client sees is unchanged. Wire shape (`{"refreshed": {...}}`) is unchanged; single-tenant is unchanged because the store is already unscoped there.
- Docs: `docs/dev/environment-reference.md` rows for `CFACTORY_{AIFACTORY,PFACTORY,TFACTORY}_TOKEN` and `CFACTORY_MODEL_GATEWAY_{URL,TOKEN}` say they are on `main` since the 2026-11-17 promotion (f4f6c9d, #510 and #513 included); the ORG_TOKENS row notes that refresh writes like polling. The post's "In progress" section and its head sentence, and the roadmap sentence, become dated facts for 2026-11-17 and point at the merged-PR list instead of naming a head.

## Alternatives rejected

- Hydrate into `scoped(default)`: global correlation-key uniqueness makes a refresh fail with `IntegrityError` on a row an earlier refresh filed under another tenant (codex finding).
- Skip refresh when the caller's tenant is not `default`: breaks the cockpit's manual refresh for every tenant.
- Tenant-aware polling (per-key hydrate into that key's tenant): larger, belongs with #517's websocket binding.

## Risks

Rows already misfiled under a caller's tenant by the old behaviour stay there (updates find them, new ones go to `default`); `scripts/retenant_work_items.py` moves them. Polling itself is still not tenant-isolated: a documented limit of this change.

## Verification

New test in `tests/test_refresh.py`: multi-tenant on, caller store scoped to tenant `b`, refresh hydrates a key; the row's tenant is `default`, the scoped `b` view does not list it, an unscoped view does, and a second refresh with the key pre-filed under `b` returns 200 and updates it in place. Existing suite and ruff pass.

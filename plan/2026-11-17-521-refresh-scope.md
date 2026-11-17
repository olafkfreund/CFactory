---
status: draft
issue: 521
spec: spec/2026-11-17-521-refresh-scope.md
coders:
  1-3: lead (three small interdependent edits in one worktree; below the handoff threshold)
reviewers:
  - codex
---

# Plan: scope `/api/refresh` writes per tenant and refresh the stale docs

Approved decisions: `/api/refresh` hydrates and reconciles through `store.unscoped()` (same view as `_poll_loop`), the injected store still feeds `broadcast_snapshot`; reconcile stays deployment-wide, no prune calls added; no wire change; docs state dated facts. Base `dev`.

## Steps

1. `apps/backend/cfactory/store.py` (after `scoped`, ~l.453): add `unscoped()` returning `copy.copy(self)` with `_tenant = None`. `apps/backend/cfactory/routes_events.py` `refresh`: `db = store.unscoped()` at the top; pass `db` to `hydrate`, `reconcile_snapshot`; keep `store` for `broadcast_snapshot` → `pytest tests/test_refresh.py tests/test_ws_broadcast.py`
   Traps: `live_ids_all_orgs` and the AdapterError reference handling stay as they are.
2. `tests/test_refresh.py`: add the multi-tenant test from the spec (monkeypatch `cfactory.routes_events.get_settings`; caller store `store.scoped("b")`; rows of tenants `b` and `c`; assert tenants, status update in place, third-tenant reconcile both ways) → `pytest tests/test_refresh.py`
   Traps: the settings stub needs `multi_tenant` and `extra_tokens_for`; `broadcast_snapshot` uses `get_settings` from `cfactory.routes_events` too.
3. Docs: `docs/dev/environment-reference.md` (rows `CFACTORY_{AIFACTORY,PFACTORY,TFACTORY}_TOKEN`, `CFACTORY_MODEL_GATEWAY_{URL,TOKEN}`, ORG_TOKENS), `docs/roadmap.md`, `docs/_posts/2026-11-15-what-shipped-august-to-november.md` → grep shows no "dev only"/"not on `main`" claim for #510/#513 and no claimed `main` head older than f806a27.
   Traps: state dated facts ("as of 2026-11-17"), not a head; keep table rows on one line.

## Tests

`PYTHONPATH=apps/backend pytest` (CI backend lane) and `ruff check` / `ruff format --check` on touched files; `python scripts/ratchet_lint.py` if CI runs it (see `justfile`).

## Rollback

Revert the PR; no data migration. Rows misfiled by the old behaviour are moved with `scripts/retenant_work_items.py`.

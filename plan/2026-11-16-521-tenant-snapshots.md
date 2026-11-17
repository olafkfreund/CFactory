---
status: approved
issue: 521
spec: spec/2026-11-16-521-tenant-snapshots.md
coders:
  1-4: lead (small, interdependent edits in one worktree)
reviewers:
  - codex
---

# Plan: tenant-scoped snapshot broadcasts and key-binding migration

Approved decisions: per-client filtering in the manager (no payload change); explicit retenant script, not a startup migration; docs state dated facts. Base `dev`.

## Steps

1. `apps/backend/cfactory/ws.py`: add `ConnectionManager.broadcast_snapshot(store, multi_tenant)` as in the spec → `pytest tests/test_ws_broadcast.py`
   Traps: keep `broadcast` signature; DB read via `run_in_threadpool`.
2. `progress.py` (`_poll_loop` ~l.222) and `routes_events.py` (`refresh` ~l.116): replace the inline snapshot broadcast with the new call; refresh passes its injected `store` (scoped() re-scopes any store; keeps the test seam). Add tests in `tests/test_ws_broadcast.py` → `pytest tests/test_ws_broadcast.py tests/test_multi_tenant.py`
   Traps: `store_dep` is already scoped under multi-tenant; `broadcast_snapshot` re-scopes per tenant itself.
3. `store.py`: `WorkItemStore.retenant(tenant, keys, from_tenant="default")` (skips keys owned by another tenant); `scripts/retenant_work_items.py`; test in `tests/test_tenant_scoping.py` → pytest.
4. Docs: `docs/dev/environment-reference.md`, `docs/roadmap.md`, `docs/_posts/2026-11-15-what-shipped-august-to-november.md` → `mkdocs`-free: grep that `#509` head claims are gone.

## Tests

`PYTHONPATH=apps/backend apps/backend/.venv/bin/pytest` and `ruff check` / `ruff format --check` on touched files.

## Rollback

Revert the PR; `retenant` is explicit, undo by running it with `--tenant default --from-tenant <tenant>` and the same keys file.

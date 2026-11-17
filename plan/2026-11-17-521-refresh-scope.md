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
2. `tests/test_refresh.py`: add `test_refresh_in_multi_tenant_writes_like_polling` (monkeypatch `cfactory.routes_events.get_settings` to a stub with `multi_tenant=True` and `extra_tokens_for=lambda s: []`; override `store_dep` with `lambda: store.scoped("b")`). Setup: upstream (mock transport) lists tasks for keys K1 (new) and K2 (stored earlier under tenant `b` with status `coding`, upstream now `done`); a tenant `c` row with a running stage for task T3 listed upstream, and another `c` row T4 running but absent upstream. Assert: status 200 and wire shape `{"refreshed": {...}}`; K1's row has tenant `default`, is not in `store.scoped("b").list()` and is in `store.list()`; K2 stays tenant `b` with status `done` (update in place, no IntegrityError); T3 stage still running; T4 stage cleared as polling would → `PYTHONPATH=apps/backend pytest tests/test_refresh.py`
   Traps: use separate keys per case (correlation keys are globally unique); a changed status is required for the update to apply; `broadcast_snapshot` also reads `get_settings` from `cfactory.routes_events`, so the stub needs `multi_tenant` only.
3. Docs: `docs/dev/environment-reference.md` (rows `CFACTORY_{AIFACTORY,PFACTORY,TFACTORY}_TOKEN`, `CFACTORY_MODEL_GATEWAY_{URL,TOKEN}`, ORG_TOKENS), `docs/roadmap.md`, `docs/_posts/2026-11-15-what-shipped-august-to-november.md` → grep shows no "dev only"/"not on `main`" claim for #510/#513 and no claimed `main` head older than f806a27.
   Traps: state dated facts ("as of 2026-11-17"), not a head; keep table rows on one line.

## Tests

From the worktree root, base `origin/dev`:

```bash
PYTHONPATH=apps/backend pytest -v
python scripts/ratchet_lint.py --base origin/dev --package apps/backend/cfactory --package scripts
python scripts/ratchet_lint.py --tool mypy --base origin/dev --package apps/backend/cfactory --package scripts
ruff check --config ruff.toml apps/backend/cfactory scripts
ruff format --check --config ruff.toml apps/backend/cfactory scripts
PYTHONPATH=apps/backend python scripts/dump_api_contract.py --check
```

## Rollback

Revert the PR; no data migration. Rows misfiled by the old behaviour are moved with `scripts/retenant_work_items.py`.

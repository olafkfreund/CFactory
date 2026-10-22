---
status: approved
issue: 490
spec: spec/2026-10-22-490-audit-after-id.md
---

# Plan: /api/audit after_id paging for the SIEM forwarder

Self-contained summary of the approved decisions:

- `AuditStore.list(limit=100, after_id=None)`: with `after_id`, rows with
  `id > after_id`, ascending, limited; without it, unchanged newest-first.
- `GET /api/audit?after_id=&limit=`: `after_id` int >= 0 optional, `limit`
  1..500 default 100, validated by FastAPI `Query`; keys unchanged.
- openapi.yaml documents the params. Forwarder loop is out of scope (factory-gitops).

## Steps

1. apps/backend/cfactory/audit.py:449-453 (`AuditStore.list`): add
   `after_id: int | None = None`; when set, filter `AuditEntry.id > after_id`
   and order by `id.asc()`. Update the docstring.
   → verify by `pytest tests/test_audit.py -q`.
   Traps: keep the no-arg query byte-for-byte equivalent; the Audit view polls it.
2. apps/backend/cfactory/routes_actions.py:103-135 (`list_audit`): add
   `after_id: Annotated[int | None, Query(ge=0)] = None` and
   `limit: Annotated[int, Query(ge=1, le=500)] = 100`; import `Query` from
   fastapi; call `audit.list(limit=limit, after_id=after_id)`. Add a docstring
   paragraph on paging.
   → verify by `pytest tests/test_audit.py -q`.
   Traps: `/api/audit` is in the `read` scope; do not change auth.
3. openapi.yaml `/api/audit` get: add `parameters` for `after_id` and `limit`.
   → verify by `python -c "import yaml;yaml.safe_load(open('openapi.yaml'))"`.
   Traps: none.
4. tests/test_audit.py: add `test_audit_after_id_pages_every_row` (250 rows
   via `audit.record`, page `after_id` with `limit=100` until short page, assert
   ids == sorted all ids) and a 422 check for `limit=501`.
   → verify the test fails on origin/dev code and passes after.

## Tests

`cd apps/backend && pytest ../../tests/test_audit.py -q` (or the repo's
configured pytest root), `ruff check` on touched files, ratchet lint if present.

## Rollback

Revert the PR; the params are optional so no client depends on them until the
factory-gitops forwarder change lands.

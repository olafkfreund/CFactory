---
status: draft
issue: 490
intent: intent/2026-10-22-490-audit-after-id.md
---

# Spec: /api/audit after_id paging for the SIEM forwarder

## Design (chosen; the user's decision for this wave)

- `AuditStore.list(limit: int = 100, after_id: int | None = None)`:
  with `after_id` set, `where(AuditEntry.id > after_id).order_by(AuditEntry.id.asc()).limit(limit)`;
  without it, the current newest-first query, unchanged.
- `GET /api/audit` gains two optional query params: `after_id: int >= 0` and
  `limit: int` in 1..500 (default 100), passed straight through. FastAPI's
  `Query` validation rejects out-of-range values with 422. Response keys
  unchanged (`count`, `attribution`, `entries`).
- openapi.yaml documents both params.
- Forwarder loop (read from high-water mark until a short page) is a separate
  factory-gitops PR, not part of this change.

## Alternatives rejected

- `since` timestamp: ts is not unique and is subject to the DST/timezone
  issues already seen in this table; the autoincrement id is monotonic and is
  exactly the forwarder's high-water mark.
- Raising the fixed limit: only moves the gap.
- Opaque cursor tokens: more code, no benefit for a single integer key.
- Unbounded limit: turns a polled read into a full-table scan.

## Risks

- Ascending order only applies when `after_id` is given; a caller passing only
  `limit` still gets newest first. Documented in openapi.
- `limit` cap 500 bounds per-request cost.

## Verification

- New test: insert 250 rows, page with `after_id` and `limit=100` until a short
  page; assert every id seen exactly once in ascending order.
- Existing `tests/test_audit.py` passes (default behaviour unchanged).
- ruff, `scripts/ratchet_lint.py` if present.

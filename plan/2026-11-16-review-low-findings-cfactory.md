---
status: approved
issue: 517
spec: spec/2026-11-16-review-low-findings-cfactory.md
coders:
  1: coder   # small, implemented directly
reviewers:
  - codex
---

# Plan: tenant-bound extra org sockets

Approved decisions: `tenant=key` syntax; scope only when multi-tenant is on; sockets carry a
tenant; REST/poll broadcast unchanged.

## Steps

1. `config.py` (`extra_org_bindings_for`), `ws.py`, `routes_ws.py`, `upstream_ws.py`, env
   reference doc -> `PYTHONPATH=apps/backend pytest tests/test_upstream_ws.py`.
2. Tests in `tests/test_upstream_ws.py` -> full `pytest`, `ruff check`.

## Tests

New tests fail on the unscoped code.

## Rollback

Revert the commit.

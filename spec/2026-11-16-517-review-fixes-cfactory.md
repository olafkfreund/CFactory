---
status: draft
issue: 517
intent: intent/2026-11-16-517-review-fixes-cfactory.md
---

# Spec: reconcile across all orgs

## Design

Add `BaseHTTPAdapter.with_token(token)` (same endpoint, other key) and
`live_ids_all_orgs(adapter, items, extra_tokens)` in `adapters/__init__.py`: union the primary
task ids with each extra key's `list_items()`; return `None` if any extra listing raises
`AdapterError`. `progress.poll_progress_once` and `routes_events.refresh` call it and run
`reconcile_snapshot` only when the result is not `None`. With no extra keys the result equals
today's id set.

## Alternatives rejected

- Age-based reconcile of unlisted rows: would blank quiet but live org B tasks.
- Per-org tagging of store rows: schema change, larger than a focused fix.

## Risks

One extra list call per extra key per poll. A failing extra org suspends reconcile (safe default).

## Verification

`tests/test_reconcile_extra_orgs.py`: stale row cleared and org-B row kept (poll and refresh);
no clearing when an extra org is down. Full pytest and ruff.

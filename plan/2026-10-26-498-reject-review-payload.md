---
status: approved
issue: 498
spec: spec/2026-10-26-498-reject-review-payload.md
---

# Plan: reject review sends a valid apply-correction body

Decisions from the approved spec: send `fix_request_md`, `confirm: true`, `correlation_key` and a `triage` block (`source: "triage"`, one `cockpit-review` failing item carrying the note); keep the top-level `source: "cockpit"` and `reason`; leave the PFactory branch alone; test against a pinned copy of the schema.

## Steps

1. apps/backend/cfactory/actions.py `propose_reject_review` (AIFactory branch): build the payload above → verify by `pytest tests/test_actions.py`
   Traps: keep the PFactory branch byte-identical.
2. tests/data/aifactory-handback-triage.schema.json (byte-identical to AIFactory `apps/backend/qa/handback_triage.schema.json` @ 74b4b9b9bed20c5d492d3e8a7b5067b0deb4bc2a) and tests/test_reject_review_contract.py: validate the payload's `triage` against the schema, and check the body has the fields `ApplyCorrectionRequest` requires and has `confirm` true. Update the payload assertion in tests/test_actions.py → verify by `pytest tests/test_reject_review_contract.py tests/test_actions.py`, ruff
   Traps: jsonschema is test-only; use `pytest.importorskip`, as test_fleet_manifest_conformance.py does.

## Tests

The new contract test fails on the old `{reason, source}` payload: it has no triage block and no `fix_request_md`.

## Rollback

Revert the PR.

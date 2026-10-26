---
status: draft
issue: 498
author: olafkfreund
---

# Intent: reject review for a build-stage item sends a valid apply-correction body

## Problem

The cockpit's reject review for an AIFactory (build-stage) item always fails with a 422. `propose_reject_review` posts `{"reason", "source"}` to `POST /api/tasks/{id}/apply-correction`. AIFactory's `ApplyCorrectionRequest` requires `fix_request_md`, and since AIFactory 3.10.9 (`APP_REQUIRE_CORRECTION_TRIAGE` on by default) it also requires a `triage` block. Without `confirm: true` the endpoint only previews, so even a body that validated would not start the fixer.

## Proposed outcome

A build-stage reject sends `fix_request_md` built from the reviewer's note, `confirm: true`, and a `triage` block that AIFactory's validator and published schema both accept. The QA Fixer runs on the reviewer's note.

## Affected users and systems

CFactory cockpit (apps/backend/cfactory/actions.py) → AIFactory apply-correction. The plan-stage PFactory reject branch is unchanged.

## Constraints

Smallest change. No new runtime dependency. CFactory CI has no AIFactory checkout, so the contract test uses a pinned copy of the schema.

## Open questions

None.

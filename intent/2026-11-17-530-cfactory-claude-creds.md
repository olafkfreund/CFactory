---
status: draft
issue: 530
author: olafkfreund
---

# Intent: Give the cfactory pod a Claude login

## Problem

The cfactory Deployment (factory-gitops `apps/cfactory`) has no cred-sync sidecar and no Claude credential. CFactory's copilot (`apps/backend/cfactory/copilot/service.py`, `_default_runner`) calls Claude through the Claude Agent SDK, and `copilot_provider` defaults to `claude`. In the pod the SDK answers "Not logged in". Release teams hit this when verifying with an SDK query. No other CFactory code calls Claude (grep of origin/dev: only the copilot).

## Proposed outcome

After Argo syncs, the cfactory pod holds a fresh Claude OAuth credential the same way pfactory/tfactory/aifactory do (shared `factory-cli-creds` Secret kept fresh by cred-broker, copied in by a cred-sync sidecar), and a copilot question with provider `claude` is answered.

## Affected users and systems

CFactory cockpit copilot users; factory-gitops `apps/cfactory`; release verification.

## Constraints

- No new secrets and no credential values handled; reuse `factory-cli-creds`.
- Copy the existing wiring; keep the cred-sync script byte-identical (the guard test enforces this).
- Keep readOnlyRootFilesystem and non-root settings.

## Open questions

None. Decision (owner delegation 2026-11-17): a feature needs Claude, so wire credentials rather than skip the SDK query.

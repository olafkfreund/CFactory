---
name: cfactory-deploy-runbook
description: Guide an operator through CFactory's promote-and-deploy runbook, from the dev to main promotion to a verified, signed pair of images in the cluster, and its rollback.
when_to_use: When the user asks how CFactory is released or deployed, why a change is not live, how to verify what is running, or how to roll back a bad CFactory build.
---

# CFactory deploy runbook

[RELEASE.md](../../../RELEASE.md) is the source of truth and describes
[deploy.yml](../../../.github/workflows/deploy.yml). CFactory has no version number, tag,
GitHub Release or changelog gate: a release is a commit on `main`, named by `sha-<short>`.

1. **Land on `dev`.** Feature PRs target `dev`; nothing deploys from `dev`.
2. **Promote.** `gh pr create --base main --head dev --title "chore: promote dev to main"`. Required
   checks are `Backend pytest` and `Frontend typecheck + build`. Merging triggers the deploy.
3. **Deploy.** A push to `main` (not `docs/**` or root `*.md`) runs `deploy.yml`: the test gate,
   then builds `ghcr.io/olafkfreund/cfactory` and `cfactory-frontend` as `sha-<short>`, Trivy-scans
   both images and the frontend lockfile, signs both with cosign, attests SPDX and CycloneDX SBOMs,
   self-verifies, runs `assert-signature-location.sh`, then bumps both images in `factory-gitops`
   `apps/cfactory/manifests`. Argo CD syncs it. A post-deploy PARR seam smoke check follows.
4. **Prerequisites (operator-owned secrets).** `GITOPS_PAT` (without it images are pushed and the
   bump is skipped), `GHCR_PAT` or package write access for `GITHUB_TOKEN`, and `FACTORY_TOKEN`
   (without it the job fails). An agent never mints, reads or prints these.
5. **Verify.**
   - `gh run list -R olafkfreund/CFactory --workflow deploy.yml -L 3`, then `gh run view <id>`.
   - The seam smoke step is soft (`continue-on-error`): read that step's result, not only the job.
   - Pin landed: `grep -rn "sha-" <factory-gitops clone>/apps/cfactory/manifests`
     shows `sha-<short>` for backend and frontend.
   - Signatures: `cosign verify` both images with the identity regexp `SIGNER_IDENTITY` and issuer
     `https://token.actions.githubusercontent.com` from `deploy.yml`.
   - Running (read-only): `kubectl -n factory get deploy`; the backend probes `/health`.
   - Copilot: ask a copilot question and check `kubectl -n factory logs deploy/cfactory -c cred-sync`
     shows no refusal. "Not logged in" means the sidecar or the `factory-cli-creds` Secret is missing,
     not a code regression.
   - Drift: `deploy-drift.yml` flags a `main` commit that never reached the pin.
6. **Rollback.** There is no rollback command in this repo. The operator reverts or re-pins BOTH
   images in `factory-gitops` `apps/cfactory/manifests` to the last good `sha-<short>` and lets
   Argo CD reconcile. Then repeat step 5.

An agent never pushes to `factory-gitops`, never runs `kubectl apply`, `delete` or `rollout`, and never
touches the live cluster beyond read-only `get` and `logs`.

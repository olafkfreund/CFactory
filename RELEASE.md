# Release Process

CFactory has no tagged releases, no version number, and no
`scripts/bump-version.js`. Do not copy a release process from a sibling
repo — read this instead; it describes what the workflows in
`.github/workflows/` actually do.

## The model: push-to-`main` deploys

| Branch | Purpose | What happens on push |
|--------|---------|-----------------------|
| `dev`  | Integration branch — default branch, where feature PRs land | `test.yml` runs; nothing deploys |
| `main` | Release branch — only receives promotion merges from `dev` | `test.yml` runs, then `deploy.yml` builds, signs, and ships |

There is no tag, no GitHub Release object, and no changelog entry gating a
deploy. The unit of release is a commit on `main`, identified by its short
SHA.

## Promoting `dev` to `main`

```bash
git fetch origin
gh pr create --base main --head dev --title "chore: promote dev to main"
```

Open the promotion PR, let CI run, get it reviewed and merged like any
other PR (`main`'s branch protection requires 1 approving review — see
CONTRIBUTING.md's Maintainers section for the full protection table).
Merging it is what triggers the deploy below.

## What `deploy.yml` does on push to `main`

`.github/workflows/deploy.yml` — "Deploy (CD -> ArgoCD)" — is the entire
release pipeline:

1. **Re-runs `test.yml` as a gate.** `build-push-bump` only starts if the
   reusable test workflow passes: `Backend pytest`
   (`PYTHONPATH=apps/backend pytest -v`) and `Frontend typecheck + build`
   (`npm run typecheck` then `npm run build`).
2. **Builds two images** — backend (`Dockerfile`) and cockpit frontend
   (`apps/frontend-web/Dockerfile`) — and tags each `sha-<short-sha>` and
   `latest`, pushed to `ghcr.io/olafkfreund/cfactory` and
   `ghcr.io/olafkfreund/cfactory-frontend`.
3. **Scans both images with Trivy** (fixable HIGH/CRITICAL,
   `--ignore-unfixed`) and separately scans the frontend lockfile, since the
   built image never contains `node_modules`. Any of the three scans
   failing stops the release before anything reaches the cluster.
4. **Signs both images with cosign** (keyless, GitHub OIDC) and
   **attests dual SBOMs** (SPDX + CycloneDX), then verifies its own
   signature and attestations before continuing — a broken signing step
   fails closed rather than shipping an unverifiable image.
5. **Bumps `factory-gitops`** (`apps/cfactory/manifests`) to the new SHA via
   `kustomize edit set image`, so ArgoCD picks up the change and deploys to
   the p510 k3d cluster. This step needs the `GITOPS_PAT` repo secret; if
   it isn't set, the workflow pushes the images and leaves a notice instead
   of failing.
6. **Runs a post-deploy PARR seam smoke check** against the live services.
   This step is soft (`continue-on-error`) so a regressed seam cannot roll
   back a deploy that already shipped, but a missing `FACTORY_TOKEN` fails
   the job outright — a check that silently never ran must not read as
   green.

`paths-ignore: ['docs/**', '*.md']` on the trigger means a root-level
markdown-only push (like this one) does not fire a deploy on its own.

## Versioning

There isn't one. If you need to refer to a specific piece of code that
shipped, use the git SHA (`sha-<short-sha>`, matching the image tags) or
the PR/issue number, not a semantic version. If CFactory later needs real
versioning (e.g. for an external API contract), that is a separate
decision — do not invent a scheme here to fill the gap.

## Rollback

There is no rollback command in this repo. Revert or bump `factory-gitops`
(`apps/cfactory/manifests`) to a previous known-good image SHA and let
ArgoCD reconcile; that repo, not this one, is the source of truth for what
is currently deployed.

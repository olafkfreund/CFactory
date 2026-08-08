# Contributing to CFactory

CFactory is the agentic control-tower cockpit over the PARR pipeline — one pane
of glass across PFactory, AIFactory and TFactory.

## TL;DR

1. Fork → branch from `dev` → make your change → PR back to `dev`.
2. Sign your commits (`git commit -s`) and follow conventional-commit subjects.
3. CI must be green before review.

## How to ask for help

[SUPPORT.md](SUPPORT.md) is the full map. The short version:

- **Questions / discussion** → [open an issue](https://github.com/olafkfreund/CFactory/issues/new). This repo does **not** have GitHub Discussions enabled, so there is no discussion forum to point you at.
- **Bugs** → [open an issue](https://github.com/olafkfreund/CFactory/issues/new)
- **Security issues** → follow [SECURITY.md](SECURITY.md); do **not** open a public issue. There is no security email address for this project — do not send a report to an address you found elsewhere and assume it arrives.

## Development setup

Prereqs: **Python 3.13**, **Node.js 22+**, **git**, **uv**. Everything is
available inside the Nix dev shell (`nix develop`, or direnv), and the `just`
recipes assume you are in it.

```bash
git clone https://github.com/olafkfreund/CFactory.git
cd CFactory
nix develop            # or: direnv allow
just bootstrap         # apps/backend/.venv + backend and test deps
just ui-install        # frontend deps
```

Run it — backend on 3111, cockpit dev server on 3110 (which proxies `/api` and
`/health` to 3111):

```bash
just run               # terminal 1: backend
just ui                # terminal 2: cockpit
```

Open <http://localhost:3110>. `just --list` shows every recipe.

## Branching workflow

| Branch         | Purpose                                  | PR target |
|----------------|------------------------------------------|-----------|
| `feature/*`, `fix/*`, `chore/*` | Your work | `dev`     |
| `dev`          | Integration branch — pre-release work    | `main`    |
| `main`         | Stable; deploys are cut from here        | —         |

`dev` is the working branch. Branch from `origin/dev`, sign your commits, and
open PRs against `dev`. `main` is a release branch and only receives promotion
merges from `dev`. Do **not** branch new feature work from `main`.

Hotfixes can PR straight to `main` but require a maintainer review.

```bash
git fetch origin
git checkout -b fix/short-description origin/dev
# work
git commit -s -m "fix: brief subject in imperative voice"
git push -u origin fix/short-description
gh pr create --base dev
```

Only `main` deploys: `deploy.yml` fires on push to `main`, never on `dev`. Work
merged to `dev` is not live until it is promoted.

## Commit messages

[Conventional commits](https://www.conventionalcommits.org/), single-line
subject, imperative voice.

```
feat: add task-creation wizard
fix: handle empty upstream response in the token roll-up
docs: clarify the multi-tenant guide
chore: bump the frontend lockfile
```

Sign every commit with the **Developer Certificate of Origin** (`-s`). PRs
without sign-off will be asked to amend.

## Tests and gates

Run before you push:

```bash
just test              # backend pytest
just ui-build          # frontend typecheck + production build
```

Two checks are required on both `dev` and `main`, and both come from
`.github/workflows/test.yml`:

| Check | What it runs |
|-------|--------------|
| `Backend pytest` | `PYTHONPATH=apps/backend pytest -v` |
| `Frontend typecheck + build` | `npm run typecheck` then `npm run build` |

`code-quality.yml` additionally runs a per-file ruff + mypy ratchet (a changed
file may not gain violations against the PR base), a whole-repo
`ruff format --check`, and the frontend ESLint/prettier/vitest gate.

`apps/frontend-web/src/index.css` is hand-authored and deliberately outside the
prettier scope. Never run `prettier --write` over it.

### pre-commit

`.pre-commit-config.yaml` runs those same gates locally, at the same pinned tool
versions, so you find out before you push rather than after. It adds no rules of
its own — a green pre-commit is the same tools reaching the same verdict CI will.

```bash
pip install pre-commit
pre-commit install                      # commit-stage hooks
pre-commit install --hook-type pre-push # the ruff ratchet (needs both)
pre-commit run --all-files              # optional: whole tree, ~20s
```

Both `install` commands are needed. The ruff ratchet compares `origin/dev...HEAD`
with `git diff`, so it reads committed content and only means anything at push
time; the rest run per commit on the files you staged.

Frontend hooks call this repo's own npm scripts, so `just ui-install` must have
been run — otherwise they fail rather than skip, which is the correct way round.

Read the comments in that file before changing it. Two of the exclusions are
load-bearing rather than cosmetic: the vendored trees (`apps/backend/runners/
github/`, `scripts/`, `standards/`, `_contracts/`) are byte-exact hub copies
under blocking drift gates, and a whitespace fixer let loose on them turns a
green repo red with a tidy-looking diff.

## Maintainers

Branch protection on `main` and `dev` is declared as code in the Factory hub, in
[`scripts/apply_branch_protection.sh`](https://github.com/olafkfreund/Factory/blob/main/scripts/apply_branch_protection.sh)
— one engine covering all four service repos plus the hub and gitops, rather than
a copy per repo that drifts on its own. From a Factory checkout:

```bash
scripts/apply_branch_protection.sh --repo CFactory           # CHECK: report drift, write nothing
scripts/apply_branch_protection.sh --apply --repo CFactory   # WRITE the declared intent
```

Check is the **default**: it reads the live configuration, diffs it against the
declared intent, and exits non-zero on any divergence without changing anything.
Applying requires the explicit `--apply`. Either mode needs a token with admin on
the repo, because reading branch protection is an admin-only endpoint. A scheduled
job in the hub runs check mode across the fleet daily, so drift surfaces without
anyone having to remember to look.

What is protected — but read [#351](https://github.com/olafkfreund/CFactory/issues/351)
first. Three rows of this table do not match what the two branches carry today:
a third check (`vendored copies match the hub canonical (byte-exact)`) is
required on both, and `main` has no `required_pull_request_reviews` block at
all, so neither the approving review nor conversation resolution is in force.
Reconciling that means deciding what the protection *should* be and re-applying
it with an admin token, which is why it is a separate issue rather than a
correction here.

| | `main` | `dev` |
| --- | --- | --- |
| Required CI checks | `Backend pytest`, `Frontend typecheck + build` | same |
| Branch must be up to date | yes | no |
| Approving reviews | 1 | none |
| Code-owner review | no (a `CODEOWNERS` file exists; the setting that would enforce it is off) | no |
| Conversation resolution | yes | no |
| Force-push / deletion | blocked | blocked |

`dev` requires no review deliberately. It is the default branch and the one PRs
target, and a solo maintainer — or one of the factory's own agents — has nobody to
approve their own PR, so requiring one there would stall every merge; `strict`
would additionally force a rebase before each one. The CI checks are *not*
relaxed on `dev`: it is looser about review, never about tests. `main` keeps the
full set because it is the release branch and only receives promotion merges from
`dev`.

Note the check names are this repo's own (`Backend pytest`,
`Frontend typecheck + build`) and differ from the Python services'
(`backend (ruff + pytest)`). That is why the intent is declared per repo in one
shared script rather than copied into each repo — a copy carrying another repo's
check names was the subject of Factory#468.

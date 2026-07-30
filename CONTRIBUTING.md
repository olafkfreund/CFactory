# Contributing to CFactory

CFactory is the agentic control-tower cockpit over the PARR pipeline — one pane
of glass across PFactory, AIFactory and TFactory.

## TL;DR

1. Fork → branch from `dev` → make your change → PR back to `dev`.
2. Sign your commits (`git commit -s`) and follow conventional-commit subjects.
3. CI must be green before review.

## How to ask for help

- **Questions / discussion** → [GitHub Discussions](https://github.com/olafkfreund/CFactory/discussions) (or open a `question` issue)
- **Bugs** → [open an issue](https://github.com/olafkfreund/CFactory/issues/new)
- **Security issues** → email the maintainer directly; do **not** open a public issue

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

## Maintainers

Branch protection on `main` and `dev` is applied via `gh api`. Both branches
require the two checks above; `main` additionally requires one approving review
and conversation resolution.

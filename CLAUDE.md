# CLAUDE.md

Guidance for Claude Code (and any coding agent) working in the CFactory repo.

## What CFactory is

CFactory is the agentic control-tower cockpit over the PARR pipeline: one pane
of glass plus an LLM copilot across PFactory (plan), AIFactory (build) and
TFactory (verify). It is a **read-mostly observer** of the other three services
— it aggregates their state and never writes to an upstream without an explicit
human click (see "Human-in-the-loop" below).

Backend on port 3111, cockpit (frontend) on 3110.

## Layout

| Path | What lives there |
|------|------------------|
| `apps/backend/cfactory/` | FastAPI backend — routers, adapters, event store |
| `apps/backend/runners/` | Out-of-process runners, incl. the vendored `runners/github/` |
| `apps/backend/migrations/` | Alembic migrations (`just db-upgrade`, `just db-revision`) |
| `apps/frontend-web/` | React + Vite + TypeScript cockpit |
| `tests/` | Backend pytest suite (`PYTHONPATH=apps/backend`) |
| `charts/cfactory/` | Helm chart |
| `scripts/` | Ratchet lint helpers, deploy-drift checker |
| `docs/` | The Jekyll GitHub Pages site (public), not contributor docs |
| `guides/`, `techdocs/` | Operator guides and TechDocs |

## Contributing

**Branching model:** `dev` is the working branch — that's where feature work and
PRs go. `main` is a release branch that only receives promotion merges from
`dev`. Do NOT branch new feature work from `main` — always branch from
`origin/dev`.

**Workflow for contributions:**
1. Fetch and branch from `dev`: `git fetch origin && git checkout -b feat/my-feature origin/dev`
2. Make changes and commit with sign-off: `git commit -s -m "feat: description"`
3. Push to your branch: `git push -u origin feat/my-feature`
4. Create PR targeting `dev`: `gh pr create --base dev`

**Verify before PR:**
```bash
# Ensure only your commits are included
git log --oneline origin/dev..HEAD
```

Only `main` deploys — `deploy.yml` fires on push to `main` and never on `dev`.
A change merged to `dev` is not live until it is promoted. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## Commands

All recipes assume the Nix dev shell (`nix develop` or direnv).

```bash
just bootstrap     # backend venv + deps
just ui-install    # frontend deps
just run           # backend on 3111
just ui            # cockpit dev server on 3110 (proxies /api + /health to 3111)
just test          # backend pytest
just ui-build      # frontend typecheck + production build
just --list        # everything else
```

## Gates that must pass

Both of these are required checks on `dev` and `main`, from
`.github/workflows/test.yml`:

- `Backend pytest` — `PYTHONPATH=apps/backend pytest -v`
- `Frontend typecheck + build` — `npm run typecheck` then `npm run build`

`code-quality.yml` also runs a **per-file ratchet**: ruff and `mypy --strict`
over only the Python files a PR changes, and a changed file may not gain
violations against the PR base. Whole-repo strict is deliberately not blocking.
Touching a legacy hotspot means cleaning it.

Drift gates (`factory-github-drift.yml`, `verification-core-drift.yml`) assert
that vendored code still matches the pinned hub baseline. If one goes red, fix
the hub canonical and re-vendor — never patch CFactory's copy alone.

## Rules with teeth

- **No emojis** in code, docs, commit messages, or UI copy.
- `apps/frontend-web/src/index.css` is **hand-authored**. Never run
  `prettier --write` over it; it is outside the prettier scope on purpose.
- Never `git add -A`. Stage named paths, then check `git diff --cached
  --name-only` before committing — `git commit` commits the index, not the
  paths you just added.
- **Human-in-the-loop:** the copilot *prepares* upstream actions (approve plan,
  approve review, reject, recover, remove) and every write waits for an explicit
  human click, then is audited. Do not add a code path that writes to an
  upstream without that gate.
- **Billing honesty:** a `$` figure is shown only for **metered** billing modes.
  Subscription and local runs report tokens and time, never a notional dollar
  amount.
- Empty states must be honest rather than overclaiming — "no active agents"
  beats a fabricated row.

## Docs

After shipping, update the repo's own docs (`guides/`, `techdocs/`, the Jekyll
site under `docs/`). Every feature or flag should carry a user story, all its
options, and what happens when it is left unset.

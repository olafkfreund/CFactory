---
status: approved
issue: 457
spec: spec/2026-09-23-457-approve-existing-pr.md
---

# Plan: approving a task whose PR already exists must merge that PR

Self-contained summary of the approved decisions:

- Fix in AIFactory only (`apps/web-server/server/`). CFactory's
  `approve_review` (create-pr, then merge) is unchanged.
- `routes/pr.py` `create_pr_from_task`: resolve the task branch first (with
  `resolve_task_branch`, which tolerates a missing worktree), then call
  `_open_pr` (with an optional `repo` for `targetRepo`):
  - `OPEN`/`MERGED`: return success with `prUrl`, `prNumber`, `branch`,
    `baseBranch` and `existing: True`. No push, rebase or `gh pr create`.
  - `CLOSED` or no PR: today's create path. Only this path requires a
    worktree, so "No worktree found" moves here.
- `routes/worktree_merge.py` `merge_worktree`: drop the early worktree guard
  (line 1793). Resolve the branch and call `merge_pull_request` as today; apply
  the worktree check only on the no-PR local-merge fallback.
- Refusals stay refusals (not HTTP 200, per Factory#460). No auto-merge.
- Tasks already removed from AIFactory are out of scope.
- Docs: the AIFactory endpoint docs and CFactory `techdocs/api.md` (actions).

All code steps run in `/mnt/data/Source-home/GitHub/AIFactory` on branch
`fix/cfactory-457-approve-existing-pr`, taken from `origin/dev`.

## Steps

1. AIFactory tests first, in
   `apps/web-server/tests/test_approve_existing_pr.py`. Call the real handlers
   the way `test_refused_merge_is_not_http_200.py` does (`asyncio.run(...)` with
   `_access={}`), with a tmp project (projects file, spec dir) and patches on
   `get_projects_file`, `resolve_task_branch`, `run_gh_command` / `_open_pr`,
   `merge_pull_request` and `task_repo_dir`. Record every `gh` and `git` call.
   Seven cases, as in the spec:
   - create-pr, OPEN PR: success, same `prNumber`, `existing`, and no
     `pr create` or push recorded;
   - create-pr, MERGED PR: success;
   - create-pr, no worktree + OPEN PR: success;
   - create-pr, no worktree + no PR: "No worktree found";
   - merge, no worktree + OPEN PR: `merge_pull_request` called, success;
   - merge, no worktree + no PR: "No worktree found";
   - merge, PR refused: `success: False`, not a 200.
   → verify by running `apps/backend/.venv/bin/pytest
   apps/web-server/tests/test_approve_existing_pr.py -q -o asyncio_mode=auto`
   on the unchanged code: the OPEN, MERGED, no-worktree + OPEN PR, and
   merge-without-worktree cases fail; the two "still" cases and the refusal
   case pass.

2. `services/approval.py`: give `_open_pr` an optional `repo: str | None`
   (passes `--repo`), and add a way to get the PR URL: either extend the
   `gh pr list --json` fields and return it, or add a small `open_pr_url`
   helper. Keep existing callers source-compatible.
   → verify by running the existing approval tests and the new file.

3. `routes/pr.py`: reorder `create_pr_from_task` as summarised above. The
   `base_branch` logic stays as it is; the existing-PR return comes before
   fetch, stash, rebase and push.
   → verify by running the new create-pr cases green, plus
   `tests/test_create_pr_fetches_branch.py`.

4. `routes/worktree_merge.py`: remove the early guard in `merge_worktree` and
   re-apply it on the `(False, "")` fallback only.
   → verify by running the new merge cases green, plus
   `apps/web-server/tests/test_refused_merge_is_not_http_200.py` and
   `test_merge_decision_reaches_the_merge.py`.

5. AIFactory gates: the three `ci.yml` pytest invocations
   (`pytest tests/ -m "not slow"`, `pytest apps/backend`,
   `pytest apps/web-server/tests`), ruff on the changed files, and the repo's
   ratchet if one applies (`cq-ratchet.yml`).
   → verify by all of them passing locally before the PR.

6. Docs: grep AIFactory for `worktree/create-pr` and `worktree/merge` docs and
   update them to state both behaviours. In CFactory `techdocs/api.md`
   (actions section), add one line: Approve is safe to repeat and works after a
   worktree cleanup.
   → verify by grep on both repos.

7. AIFactory PR to `dev`: title `fix(approve): create-pr returns an existing
   PR; merge needs no worktree for a PR (CFactory#457)`, with a body linking
   CFactory's intent, spec and plan. Merge when CI is green. Then promote the
   way AIFactory does: a `release/<next patch>` branch from `dev`, a
   `chore: release <ver>` commit (CHANGELOG plus the version in
   `apps/backend/__init__.py`, `apps/frontend-web/package.json`,
   `apps/web-server/openapi.yaml` and `package.json`, checked with
   `scripts/validate-release.js`), a PR to `main`, merged when green, and
   `deploy.yml` watched to success.
   → verify by `kubectl -n factory get deploy aifactory` showing the new image,
   with the rollout complete.

8. CFactory: commit the step 6 doc line and this plan's deviations (if any) on
   `fix/457-approve-existing-pr`, then PR to `dev` (`Closes #457` once the
   AIFactory release is live), merge and promote as usual.
   → verify by CI green on both PRs.

9. End to end (after step 7 is live): in `pfactory-friends-demo`, create a
   throwaway AIFactory task via the MCP that produces a PR. Remove its worktree
   (or wait for cleanup), then run CFactory's audited `approve_review` through
   `/api/actions/propose` + `/execute`.
   → verify by the PR being merged, `approve_review ok=true` in `/api/audit`,
   and a second Approve also returning ok (idempotent). Delete the throwaway
   task afterwards through CFactory's `delete_task` action.

## Amendment (approved 2026-09-23): fetch before resolving

10. AIFactory tests first, in a new file
    `apps/web-server/tests/test_approve_fetches_branch.py`, using real git: a
    bare origin that has `aifactory/<spec>`, and a project clone without that
    ref. Cases: `merge` with an OPEN PR (gh faked) succeeds; `create-pr` with
    an OPEN PR returns it.
    → verify by running them on 3.6.84 code: both fail with "Could not
    determine task branch".
11. `routes/pr.py` and `routes/worktree_merge.py`: replace
    `resolve_task_branch` with `resolve_work_ref`, taking `[0]` as the branch.
    Update `test_approve_existing_pr.py` to patch the new name.
    → verify by the new tests, the #457 tests and `test_create_pr_fetches_branch.py`
    passing.
12. Gates as in step 5 (three suites, ruff, `cq_ratchet` ruff and mypy).
13. AIFactory PR to `dev`, then release `3.6.85` as in step 7, then the deploy.
14. Step 9 rerun on the existing probe task `020-e2e-457-approve-probe`
    (human_review, branch pushed, no PR, no worktree): Approve, then Approve
    again, and check the audit. Then delete the probe task and its demo-repo
    file if merged.

## Deviations

- Step 3: the existing-PR shortcut runs only for GitHub projects. `find_pr`
  speaks `gh`, and `create-pr` also serves GitLab and Azure DevOps through
  `_use_provider_api` / `_get_project_provider`. Those providers keep today's
  path. An unknown provider (lookup raises) also keeps today's path.
- Step 3: the handler's existing lazy `from .github import ...` moved up to
  before the check, instead of adding a second lazy import (strict-ruff ratchet
  PLC0415).
- Step 6: the AIFactory "docs" are the two handler docstrings, which feed the
  generated `apps/web-server/openapi.yaml`. It was regenerated with
  `scripts/generate-openapi-spec.py` because `techdocs.yml` fails on a stale
  copy.
- Code steps ran in a separate git worktree of AIFactory, because the main
  checkout had unrelated uncommitted work (`openapi.yaml` on
  `fix/qa-approval-needs-evidence`). That work was left untouched.

## Tests

```bash
cd /mnt/data/Source-home/GitHub/AIFactory
apps/backend/.venv/bin/pytest apps/web-server/tests/test_approve_existing_pr.py -q -o asyncio_mode=auto
apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto
apps/backend/.venv/bin/pytest tests/ -m "not slow" -q
apps/backend/.venv/bin/pytest apps/backend -q -o asyncio_mode=auto
```

## Rollback

Revert the AIFactory fix commit on `dev` and cut a new patch release. There is
no data or config change. CFactory's only change is a doc line.

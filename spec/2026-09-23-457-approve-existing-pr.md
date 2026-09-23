---
status: draft
issue: 457
intent: intent/2026-09-23-457-approve-existing-pr.md
---

# Spec: approving a task whose PR already exists must merge that PR

Decisions carried from the approved intent:

- The fix lives in AIFactory only (option a). CFactory's `approve_review`
  (create-pr, then merge) stays as it is.
- Tasks already removed from AIFactory (no spec dir) are out of scope.
- No auto-merge. Approve still needs an explicit click, and a refused merge is
  still reported as a refusal (Factory#460, AIFactory#1076).

## Design

All changes are in AIFactory `apps/web-server/server/`, reusing helpers that
already exist:

- `services/approval.py` `_open_pr(project_path, branch)` returns the most
  recent PR for a branch as `(number, state)`.
- `services/approval.py` `merge_pull_request(project_path, branch)` merges the
  open PR, treats an already-merged PR as success, and reports a GitHub
  refusal (#1076).
- `services/task_branch.py` `resolve_task_branch(...)` resolves the task
  branch without a worktree: it checks `worktree_path.is_dir()`, then the
  branch recorded at dispatch, then branches found locally or on origin.

### 1. `routes/pr.py` `create_pr_from_task`: return the existing PR

Today, "PR already exists" is a failure (`gh pr create` stderr, around line
424), and a missing worktree fails early (line 119).

Change:

a. Move the worktree check after branch resolution. Compute `base_branch`
   (unchanged logic) and resolve the branch with
   `resolve_task_branch(worktree_path=<path or the expected worktree dir>, ...)`.
   That function already copes with a missing directory.
b. Before any push, fetch, rebase or `gh pr create`, look up
   `_open_pr(project_path, branch)`:
   - `OPEN` or `MERGED`: return `{"success": True, "data": {"prUrl", "prNumber",
     "branch", "baseBranch", "existing": True}}` with no push and no rebase.
     This is idempotent. `prUrl` comes from a `gh pr view <n> --json url`
     lookup, or from `_open_pr` extended to return the URL.
   - `CLOSED`: fall through to the normal create path. A new PR is a
     legitimate outcome, and `gh` will open one.
   - none: the normal path.
c. Only the normal path (no PR yet) needs a worktree or build clone to push
   from. The "No worktree found" error moves there, where it is actually true.

Cross-repo PRs (`options.targetRepo`): `_open_pr` must search the target repo
when it is set (add an optional `repo` argument that passes `--repo`).
Otherwise an existing cross-fork PR would not be found, and behaviour would
stay as it is today.

### 2. `routes/worktree_merge.py` `merge_worktree`: no worktree needed for a PR merge

Today the guard at line 1793 returns "No worktree found" before the #1076 PR
merge path is reached.

Change: delete the early guard. Resolve the branch with `resolve_task_branch`
as it already does (it tolerates a missing directory), and call
`merge_pull_request` as it already does. Re-apply the worktree check only on
the historical local-merge fallback, `(False, "")` meaning no PR for the
branch, because that path runs `git merge <branch>` and needs the branch
locally. The merge-blocking-file cleanup that runs before it stays on that
path too.

### 3. CFactory: no code change

With (1), the create-pr step succeeds on an existing PR, and CFactory's
follow-up `merge` step runs, which with (2) works without a worktree. The audit
row is written by the existing `/api/actions/execute` path. The `existing`
field is informational; CFactory ignores unknown fields.

### Docs

- AIFactory: the create-pr and merge endpoint docs (wherever they are
  documented; grep for `worktree/create-pr`) state both behaviours: an existing
  PR is returned, and merge works on a PR with no worktree.
- CFactory `techdocs/api.md`, actions section: one line saying Approve is safe
  to repeat and works after a worktree cleanup.

## Alternatives rejected

- **Treat "already exists" stderr as success after the fact.** It depends on
  parsing GitHub CLI wording, and it still pushes and rebases the branch under
  an open PR before discovering the PR exists. Looking the PR up first is
  explicit.
- **CFactory-side workaround (option b).** Rejected in the intent. It can't fix
  the missing-worktree case.
- **Always merge by branch, skipping create-pr in CFactory.** It changes
  CFactory's action contract and still leaves AIFactory's create-pr
  non-idempotent for other callers (the AIFactory UI, the MCP
  `task_create_pr`).

## Risks

- **Rebase skipped on an existing PR.** Today's create-pr rebases onto the
  latest base before pushing. When returning an existing PR it will not.
  That's correct: the PR's own checks and GitHub's mergeability decide, and
  `merge_pull_request` reports a refusal if the branch is behind or
  conflicting.
- **Wrong-branch resolution without a worktree.** `resolve_task_branch`
  validates that the recorded branch exists and falls back to discovery. It is
  the same resolver merge already uses (#1073). Low risk.
- **Where the artifacts live.** The code lands in AIFactory, but the intent,
  spec and plan live in CFactory, where #457 is tracked. The AIFactory PR links
  them and references CFactory#457.
- **Deploy order.** CFactory needs no deploy. The fix is live once AIFactory is
  promoted to `main` and deployed.

## Verification

- AIFactory unit tests (pytest, next to `tests/test_create_pr_fetches_branch.py`
  and `apps/web-server/tests/test_refused_merge_is_not_http_200.py`, with `gh`
  and `git` stubbed as those tests already do):
  - create-pr with an OPEN PR for the branch: `success: True`, same `prNumber`,
    `existing: True`, and no `gh pr create` or push call recorded;
  - create-pr with a MERGED PR: `success: True`;
  - create-pr with no worktree and an OPEN PR: `success: True` (was "No
    worktree found");
  - create-pr with no worktree and no PR: still "No worktree found";
  - merge with no worktree and an OPEN PR: calls `gh pr merge` and succeeds;
  - merge with no worktree and no PR: still "No worktree found";
  - merge with a PR that GitHub refuses: `success: False`, and the HTTP status
    is not 200 (existing #460 behaviour kept).
  Run the new tests first against the current code: all "was failing" cases
  fail.
- AIFactory's full test suite and its required CI gates green.
- End to end, after AIFactory is deployed: create a throwaway task in the demo
  repo with an open PR and a cleaned-up worktree. Cockpit Approve merges it,
  and the CFactory audit shows `approve_review ok=true`.

---
status: approved
issue: 457
author: olafkfreund
---

# Intent: approving a task whose PR already exists must merge that PR

## Problem

The cockpit's **Approve** on a code task in `human_review`
(`approve_review`, `apps/backend/cfactory/actions.py`
`propose_approve_review`) is two upstream calls to AIFactory, run in order and
stopping at the first failure:

1. `POST /api/tasks/{id}/worktree/create-pr`
2. `POST /api/tasks/{id}/worktree/merge`

It fails in two situations that are normal in practice:

- **The PR already exists.** AIFactory's create-pr runs `gh pr create` and
  reports GitHub's "a pull request for branch ... already exists" as a failure
  (`AIFactory apps/web-server/server/routes/pr.py`, around line 424). Step 2
  never runs. On 2026-09-23, task `018` (pfactory-friends-demo#50) could not be
  approved from the cockpit this way.
- **The worktree is gone.** Both endpoints first require the task's local
  worktree (`pr.py:119`, `worktree_merge.py:1793`), and return "No worktree
  found for this task" when it has been cleaned up. That happens even though
  `/merge` already knows how to merge the task's PR by branch name without a
  worktree (AIFactory#1076, `merge_pull_request`). This hit all 15 profile
  tasks on 2026-09-23.

So the operator falls back to merging with `gh` by hand. That write bypasses
the cockpit's human-in-the-loop audit trail, which exists precisely to record
who approved what.

## Proposed outcome

- Approve on a task whose PR is already open merges that PR. Running it twice
  is harmless.
- Approve works when the worktree has been cleaned up, as long as the branch
  and its PR exist on GitHub.
- The merge is recorded in CFactory's audit trail like any other approve.
- When GitHub refuses the merge (conflicts, failing checks, branch
  protection), Approve reports that refusal honestly, as #1076 already does,
  and never falls back to a local merge.

## Affected users and systems

- AIFactory: the `create-pr` and `merge` handlers (web-server routes). The fix
  most likely lives here, per the "fix upstream" rule.
- CFactory: `actions.py` (`propose_approve_review` and how steps chain), and
  its tests. It may need no change if AIFactory becomes idempotent.
- Operators and agents approving from the cockpit or the `parr-run` flow.

## Constraints

- Human-in-the-loop stays: Approve still happens only on an explicit click,
  and every write is audited. No auto-merge.
- Never report success for a merge GitHub declined (Factory#460: the refusal
  sits in the body, not the status line).
- Cross-repo PRs (`targetRepo`) and draft PRs must keep working.
- Changes to AIFactory go through AIFactory's own PR and gates. CFactory does
  not special-case around an upstream bug if the upstream can be fixed.

## Open questions

Resolved 2026-09-23: 1 = (a) AIFactory only; 2 = out of scope.

1. **Where the fix lives.**
   - (a) AIFactory only: create-pr returns the existing open PR as success,
     and `/merge` skips the worktree requirement when a PR exists for the
     branch. **Recommended:** it fixes every caller, not just CFactory.
   - (b) CFactory only: treat "already exists" as success and call `/merge`
     directly. Smaller, but it only fixes the cockpit, and it can't fix the
     missing-worktree case, which lives in AIFactory.
   - (c) Both: (a) plus making CFactory's approve tolerate an older AIFactory.
2. **Tasks with no worktree and no local task record** (e.g. already
   removed): out of scope? **Recommended:** yes. There is nothing to approve
   against once the task is gone.

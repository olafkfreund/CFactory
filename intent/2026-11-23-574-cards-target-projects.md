---
status: draft
issue: 574
author: olafkfreund
parent: olafkfreund/Factory#3857 (phase 3, CFactory slice)
---

# Intent: cards target registry projects on GitHub or GitLab, and card changes reach the issue

## Problem

Owner ask (2026-11-23): when creating or editing a card in CFactory you must
be able to choose the project/repo it targets. The choices are the projects
the teams actually work on, including new repos users add for coding,
planning and testing, on GitHub or GitLab. Once a repo is added, CFactory
must be able to create issues, push updates and add cards for it.

Today none of that holds:

- **No picker.** The new-card form (`BacklogView.tsx` `save()`) has no
  project or repo field. In a `/p/<slug>` view the card silently takes that
  project's first repo; on `/` it takes the tenant default. Editing cannot
  re-target a card. The backend already accepts `repository_id` on create
  and patch (`cards.py:440,471`), but the TS types omit it.
- **CFactory does not know the projects.** Its "projects" are
  `git_repository` rows registered by hand on a git connection. Live there
  are 2, `aifactory-demo` (default) and `tfactory-testing`. The link to a
  real project is a free-text `aifactory_project_id`.
  - The AIFactory project registry (#2157: `slug`, `repo_identity`
    `host/owner/repo`, team, archived) is the agreed owner of the project
    list (#3857), but no portal reads it yet.
  - A repo a user adds in AIFactory never shows up in CFactory.
- **Issue sync is one-way and create-only** (`github_sync.py`). A card that
  reaches `ready` opens an issue. After that the issue is the source of
  truth and the card mirrors it. Editing a card or closing it never updates
  or closes the issue. The sync is already provider-neutral (GitHub,
  GitLab, Azure DevOps via `IssueProvider`).

## Proposed outcome

1. **Project picker on cards.** The new-card and edit forms have a
   Project field. It lists the registry projects the tenant works on, each
   showing its repo and provider. It preselects the project from the URL
   (`/p/<slug>`), else the tenant default. The card stores the matching
   `repository_id`. Re-targeting an existing card is allowed until it has
   an issue.
2. **New repos appear without manual setup.** A project registered in
   AIFactory (by a user, a portal or the factory CLI) appears in CFactory
   within a minute. If the tenant has a git connection for that repo's
   host (github.com, gitlab.com or a self-hosted GitLab), CFactory creates
   the `git_repository` row bound to the project id. If there is no
   connection, the project is listed but marked "connect GitHub/GitLab
   first", with a link to the connection settings.
3. **Two-way issue updates.**
   - Creating a card for a project opens the issue in that project's repo,
     as today.
   - Editing the card's title, description or acceptance criteria updates
     the issue.
   - Moving the card to done closes the issue; reopening reopens it.
   - The issue → card mirror keeps working.
   - The same rules hold on GitHub and GitLab.
4. **The board reflects the projects.** The cross-project board and the
   `/p/<slug>` views list the same registry projects, so CFactory, AIFactory,
   PFactory and TFactory show one project list. Archived projects drop out
   of the picker but keep their cards.

## Affected users and systems

- Owner and teams using the CFactory backlog and planning board.
- CFactory:
  - backend: git repositories, cards, `github_sync`, a new registry
    reconciler;
  - frontend: `BacklogView`, `CardParts`, `api.ts`, `projectRoute.ts`,
    `GitConnectionsPanel`.
- AIFactory project registry: read-only, through an existing route and
  key. Nothing changes in AIFactory unless a read route is missing.
- GitHub and GitLab repos the projects point at: issues are created,
  edited, closed and reopened there.

## Constraints

- **No lockout or data loss.** Existing cards, the existing two repos and
  the tenant default keep working. Cards without `repository_id` keep the
  default-repo fallback. Hand-registered repos without a registry project
  stay usable and are shown as such.
- **Tenant stays the credential boundary** (#3857). CFactory never reads
  another tenant's projects or uses another tenant's connection.
- **Credentials stay as they are.** The reconciler reuses the tenant's
  existing verified connections and never asks for or stores new
  credentials. Connecting GitLab is still the owner's install or token
  step.
- **Issue writes are idempotent and safe.**
  - An update never clobbers a newer human edit on the provider; the rule
    is decided in the spec.
  - A failed provider call leaves the card's `github_sync_error` set and
    retries, never a half state.
  - Card writes never trigger a provider write loop with the mirror.
- **Visibility.** CFactory has no team/project authorization yet (#3857
  phase 2 for CFactory, not built). Release 1 lists every project in the
  tenant. Team filtering follows CFactory project authorization.
- **AIFactory's registry is read, never written, by CFactory** in this
  task.

## Open questions

1. **Team filtering.** The picker shows all tenant projects in release 1.
   Is that acceptable until CFactory team/project authorization exists?
   Default: yes, and open a tracked follow-up.
2. **Conflict rule for issue updates.** Default: CFactory pushes only the
   fields the card changed since the last sync, and skips a field the
   provider changed after that sync (recording a sync note).
3. **Adding a repo from CFactory itself.** Default: CFactory's "add
   repository" form picks from registry projects (the #3857 "becomes a
   picker"). A brand-new repo is registered in AIFactory (link out), not
   created from CFactory.
4. **Self-hosted GitLab host matching.** Map a project's `repo_identity`
   host to a connection by the connection's web host, derived from
   `base_url`. Default: yes. `api.github.com` maps to `github.com`, and
   GitLab `base_url` hosts map as they are.

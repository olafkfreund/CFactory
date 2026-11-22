---
status: approved
issue: 570
author: olafkfreund
---

# Intent: URL-scoped project view in CFactory (`/p/:slug`, breadcrumb, slug-keeping portal switcher)

## Problem

Under epic olafkfreund/Factory#3857 phase 4 (handover olafkfreund/Factory#3864), every Factory portal is moving to the URL-scoped project view shipped in AIFactory (`origin/dev`, PR #2159). Today CFactory's cockpit is purely state-routed at `/` (`useDashboard` holds `view` in memory, there is no router or project store, and no `ProjectSelector` or saved tab state exists):

- There is no `/p/:slug` route to view the cockpit or planning board filtered to a single project, so deep-links and browser history (reload, back/forward, two tabs on two projects) cannot scope to a project.
- The top bar has no `ProjectBreadcrumb` to switch projects or return to "All projects".
- `PortalSwitcher` (`apps/frontend-web/src/PortalSwitcher.tsx`) always links to the sibling portals' root URLs and drops the active project slug when jumping between Plan, Build, Test, and Cockpit.
- `CommandPalette` (`apps/frontend-web/src/CommandPalette.tsx`) has no "Switch project..." action.

CFactory has no project entity of its own: planning cards map to a git repository via `repository_id` (falling back to the tenant `default_repository_id`), each repository maps to an AIFactory project via `aifactory_project_id` on the tenant's git connections (`GET /api/tenants/{tenant}/git-connections`), and pipeline `WorkItem` slices carry `repo` (`owner/name`) and `correlation_key`.

## Proposed outcome

1. **Cross-project root stays at `/`:** `/` (and `/<view>`) remains the cross-project cockpit ("All projects"), as decided in the epic.
2. **Project-scoped routes under `/p/:slug[/:view]`:** The URL is authoritative. Navigating to `/p/:slug` or `/p/:slug/:view` resolves `:slug` against the projects derived from the tenant's git connections (`aifactory_project_id` + repository `project` path, using the same deterministic `slugify` / `projectSlugs` / `findProjectByRef` precedence as AIFactory: canonical slug > project id > repository slug/path alias) before mounting project-filtered views.
   - An ID or alias ref in `/p/:ref` rewrites (`history.replaceState`) to the canonical `/p/:slug`.
   - When scoped to a project, planning views (`BacklogView`, `PlanningBoard`) and pipeline views (`MissionControl`, `NeedsYouView`, `Board`, `RunningTasksView`) filter cards and work items to those belonging to the resolved project's repositories (or joined `correlation_key` / slice `repo`).
   - An unknown slug after connections finish loading renders an honest "Project not found" empty state with a link back to `/`.
   - Reload, browser back/forward (`popstate`), and two tabs open on two different project URLs each keep their own URL-derived project and view.
3. **Topbar `ProjectBreadcrumb`:** The top bar renders `ProjectBreadcrumb` showing the active project (or `All projects` at `/`), a searchable project list, and an `All projects...` action that navigates to `/`.
4. **Slug-keeping `PortalSwitcher`:** `PortalSwitcher` accepts an optional `project={{ slug, portals }}` prop and appends `/p/<encoded slug>` to the named sibling portal links when a project slug is active.
5. **Cmd-K project switching:** `CommandPalette` offers `Switch project...` (and `All projects`) to jump to `/p/:slug` or `/` from the keyboard.

## Affected users and systems

- CFactory frontend (`apps/frontend-web/src/`): `App.tsx`, `useDashboard.ts`, `dashboard.ts`, `PortalSwitcher.tsx`, `CommandPalette.tsx`, `cards.ts`, `BacklogView.tsx`, `PlanningBoard.tsx`, and new `projectRoute.ts` + `ProjectBreadcrumb.tsx` + `index.css` rules.
- Sibling portals (PFactory, AIFactory, TFactory) linked via `PortalSwitcher`.
- No backend routes, schemas, or card/board persistence are changed.

## Constraints

- **Frontend only:** Do not change the card, board, or git-connections backend.
- **Hand-rolled CSS (`Drift B`):** CFactory does not use Tailwind or vendor `shared/factory-ui` (per `Factory/shared/factory-ui/README.md` and `scripts/check_factory_ui_drift.py`). Keep `ProjectBreadcrumb`, `PortalSwitcher`, and `CommandPalette` in CFactory's Gruvbox CSS token system (`apps/frontend-web/src/index.css`, hand-authored — never run `prettier --write` on `index.css`), while matching the `shared/factory-ui` prop contracts and accessibility semantics (`role="combobox"`, `role="listbox"`, `aria-activedescendant`, Escape/Arrow/Enter).
- **No new npm dependencies:** Use the History API (`pushState`, `replaceState`, `popstate`) rather than adding `react-router-dom`.
- **No emojis** in code, docs, or UI copy.

## Open questions

1. **Grouping when multiple git repositories share one `aifactory_project_id`:** Should one project entry represent one `aifactory_project_id` (matching every repository linked to that UUID, with display name derived from the primary/first repo basename and fallback to repos without a UUID by repo path), or one entry per git repository? *Recommendation:* group by `aifactory_project_id` when set (so the slug and UUID match AIFactory's project across portals), and include unlinked repositories by their repo slug so single-host boards without AIFactory UUIDs still filter cleanly.

## Decisions (coordinator, 2026-11-21, under the owner's standing delegation)

1. One project entry per `aifactory_project_id`. It matches every repository linked to that id, matching the epic's "one project, many repositories" model. Its display name is the AIFactory project name when known, else the first repository's name.

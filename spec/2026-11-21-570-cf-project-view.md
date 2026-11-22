---
status: approved
issue: 570
intent: intent/2026-11-21-570-cf-project-view.md
---

# Spec: URL-scoped project view in CFactory (`/p/:slug`, breadcrumb, slug-keeping portal switcher)

All paths below are relative to `apps/frontend-web/src/`.

## Design

### 1. Project derivation and slug resolution (`projectRoute.ts`)

CFactory derives its project list in the browser from the existing `fetchSettings()` -> `fetchGitConnections(tenant)` response (`GitConnections` in `api.ts:711-720`), with no backend changes:

- **`CockpitProject` shape:**
  `{ id: string; name: string; slug?: string; createdAt: string; repositoryIds: number[]; repoPaths: string[] }`.
  - Repositories with `aifactory_project_id` set are grouped by `aifactory_project_id` (`id = aifactory_project_id`). The display `name` is the last path segment of the first repository's `project` (e.g. `acme/widgets` -> `widgets`), or the full `project` path if two different orgs collide on the same basename.
  - Repositories without `aifactory_project_id` form a fallback entry keyed by `id = repo:${repository.id}` and named after `repository.project`, so boards that have not linked AIFactory UUIDs yet can still filter by project.
- **Slug & ref resolution (ported from AIFactory `lib/project-route.ts`):**
  - `slugify(name, fallback)` normalizes NFKD, lowercases, and replaces non-`[a-z0-9]` runs with `-`.
  - `projectSlugs(projects)` assigns unique slugs oldest-first (`created_at`), suffixing collisions with `-${id.slice(0, 8)}`.
  - `findProjectByRef(projects, ref)` resolves in order:
    1. canonical slug from `projectSlugs(projects)`;
    2. exact `id` match (case-insensitive, so `/p/<aifactory_project_id>` resolves directly);
    3. alias match: plain `slugify(name, id)`, collision-suffixed `${slugify(name, id)}-${id.slice(0, 8)}`, or full `owner/repo` slug (`slugify(repoPath, id)`).

### 2. Route mapping and URL precedence

CFactory has no `react-router-dom`, no `ProjectSelector`, and no saved tab-state store (`useDashboard` only persists `cfactory-ticker-open` and `theme.ts` persists theme). Navigation uses a lightweight History API hook (`useRoute` in `projectRoute.ts`) over `window.location.pathname` + `popstate`.

| Old route / state | New route | Redirect / canonical rewrite |
|---|---|---|
| `/` (`view = "overview"`, cross-project) | `/` | None — `/` stays the cross-project "All projects" Mission Control board |
| `/?task=<key>` (cross-portal deep-link) | `/?task=<key>` or `/p/:slug?task=<key>` | Opens `TaskDetail` for `<key>`, then strips `?task=` via `history.replaceState` preserving pathname |
| In-memory `view` (`needs`, `backlog`, `planning`, `pipeline`, `running`, `tokens`, `audit`, `services`, `settings`) with no project | `/<view>` (or `/` for `overview`) | Unknown `<view>` segment redirects (`replaceState`) to `/` |
| Project-scoped default view | `/p/:slug` | Resolves `:slug`; renders `overview` filtered to `:slug` |
| Project-scoped view | `/p/:slug/:view` | `:view` in `NAV` (`needs`, `backlog`, `planning`, `pipeline`, `running`, `tokens`, `audit`, `services`, `settings`); `/p/:slug/overview` or unknown `:view` rewrites (`replaceState`) to `/p/:slug` |
| `/p/:id` or `/p/:alias[/:view]` (UUID or non-canonical slug) | `/p/:canonicalSlug[/:view]` | `history.replaceState` rewrites to canonical slug once git connections load |
| `/projects` | `/` | Redirects (`replaceState`) to `/` (CFactory has no separate project-admin page; git connections live in `/settings`) |
| `/settings/token` (`main.tsx:24`) | `/settings/token` | Unchanged standalone token hand-off page |

- **View gating:** On `/p/:ref[/:view]`, project-scoped views do not mount until git connections have loaded (`connectionsLoaded === true`). While loading, the content pane renders a quiet loading state; if `findProjectByRef` returns `undefined` after load, it renders an honest `"Project not found"` panel with an `"All projects"` button back to `/`.
- **Filtering rules when a project is active:**
  - **Cards (`BacklogView`, `PlanningBoard`):** a card matches the active project when its effective `repository_id` (`card.repository_id ?? connections.default_repository_id`) is in `project.repositoryIds`.
  - **Work items (`MissionControl`, `NeedsYouView`, `Board`, `RunningTasksView`, `PipelineStrip`, `EventTicker`):** a `WorkItem` matches when any stage slice (`pfactory`, `aifactory`, `tfactory`) has `repo` in `project.repoPaths` (case-insensitive) OR its `correlation_key` belongs to a card in that project.

### 3. Topbar `ProjectBreadcrumb`, `PortalSwitcher`, and `CommandPalette`

- **`ProjectBreadcrumb.tsx`:** Mirrors `Factory/shared/factory-ui/ProjectBreadcrumb.tsx` props (`projects: BreadcrumbProject[]`, `currentSlug?: string`, `onSelect: (slug: string) => void`, `onViewAll?: () => void`) and full keyboard/a11y behavior (`aria-haspopup="listbox"`, `role="combobox"`, `aria-activedescendant`, `ArrowUp`/`ArrowDown`/`Enter`/`Escape`, outside click, trigger focus restore), styled with CFactory's CSS classes (`.proj-bc*` in `index.css`). When `currentSlug` is unset (at `/`), the trigger label reads `All projects`, and the footer button `All projects...` (via `onViewAll`) returns to `/`.
- **`PortalSwitcher.tsx`:** Adds optional `project?: { slug: string; portals?: ("plan" | "build" | "test" | "cockpit")[] }`. When `project.slug` is set, sibling links in `portals` (defaulting to `["plan", "build", "test"]`) link to `${p.url.replace(/\/+$/, "")}/p/${encodeURIComponent(project.slug)}`.
- **`CommandPalette.tsx`:** Adds optional `projects?: { slug: string; name: string }[]` and `onSwitchProject?: (slug: string | null) => void`. When provided, the Actions group includes `"Switch project..."` (which switches the palette into project-filter mode without closing) and `"All projects"` (when currently under `/p/:slug`).

## Alternatives rejected

- **Vendor `Factory/shared/factory-ui/*.tsx` directly into CFactory:** `Factory/shared/factory-ui/README.md` and `scripts/check_factory_ui_drift.py` explicitly exclude CFactory because its frontend uses hand-rolled CSS tokens rather than Tailwind/shadcn classes; dropping Tailwind markup into CFactory would render unstyled DOM.
- **Make `/` a "My projects" directory page instead of the board:** The issue explicitly specifies: *"CFactory: Keep `/` as the cross-project board (decided in the epic). Add `/p/:slug` as the board filtered to one project."*
- **Add `react-router-dom`:** Adds a new dependency for a two-pattern path (`/p/:slug[/:view]` and `/[view]`) that 40 lines of History API code handle cleanly.

## Risks

- **Work items without `repo` on their service slices:** Older events may lack `slice.repo`; joining against loaded cards' `correlation_key -> repository_id` covers board-dispatched work items, and items with neither appear under `/` ("All projects").
- **Cold load of `/p/<aifactory-uuid>` before connections finish fetching:** Handled by gating the view on `connectionsLoaded` before deciding between canonical rewrite, filtered mount, or not-found.

## Verification

- Vitest (`npm test` in `apps/frontend-web`):
  - `projectRoute.test.ts`: `slugify`, `projectSlugs` collision suffixing, `findProjectByRef` precedence (canonical slug > AIFactory project UUID > repo alias), `parseRoute` / `buildPath` table, card & work-item project filtering, History API navigation (`reload`, `popstate` back/forward, canonical replace, two independent route states).
  - `ProjectBreadcrumb.test.tsx`: static markup + interaction tests (current project label, `All projects` when unset, search filtering, keyboard navigation, no delete/add controls).
  - `PortalSwitcher.test.tsx` & `CommandPalette.test.tsx`: slug-preserving sibling links and `"Switch project..."` command mode.
- CI gates: `npm run typecheck`, `npm run lint`, `npm run format:check`, `npm run build`, and `python /mnt/code/Source-home/GitHub/Factory/scripts/check_factory_ui_drift.py --self-test`.

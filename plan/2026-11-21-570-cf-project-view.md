---
status: draft
issue: 570
spec: spec/2026-11-21-570-cf-project-view.md
coders:
  1: codex   # slug-resolution, project derivation from git connections, and URL-precedence/filter logic
  2: agy     # topbar ProjectBreadcrumb, slug-keeping PortalSwitcher, and CommandPalette project mode
  3: agy     # mechanical route wiring across App.tsx, useDashboard.ts, BacklogView.tsx, and PlanningBoard.tsx
reviewers:
  - codex    # read-only diff review against this plan
  - agy      # independent second read-only review
---

# Plan: URL-scoped project view in CFactory (`/p/:slug`, breadcrumb, slug-keeping portal switcher)

Self-contained summary of approved decisions:
- **Frontend only:** no backend changes.
- **Routes:** `/` (and `/<view>`) stays the cross-project cockpit ("All projects"). `/p/:slug[/:view]` filters the cockpit and planning views to one project. `/projects` redirects (`replaceState`) to `/`. `/p/:id` or `/p/:alias` rewrites (`replaceState`) to `/p/:canonicalSlug`. `/settings/token` (`main.tsx:24`) is untouched.
- **Project derivation (`projectRoute.ts`):** derived from `fetchSettings()` -> `fetchGitConnections(tenant)`. Repositories with `aifactory_project_id` group by `aifactory_project_id`; unlinked repositories fall back to `repo:<id>`. `findProjectByRef` resolves canonical slug > project ID (`aifactory_project_id`) > alias (`slugify(name)` / collision-suffixed / `slugify(repoPath)`).
- **URL precedence & view gating:** URL is authoritative. On `/p/:ref`, views wait until git connections load (`connectionsLoaded`), rewrite non-canonical refs, and render `"Project not found"` with a link to `/` when unknown.
- **Topbar & Cmd-K:** `ProjectBreadcrumb` sits in `.topbar` next to `PortalSwitcher` with `"All projects..."` (`onViewAll` -> `/`); `PortalSwitcher` keeps `/p/:slug` on sibling links when a project is active; `CommandPalette` gains `"Switch project..."`.

All frontend paths below are under `apps/frontend-web/`.

## Steps

1. **(codex) Project derivation, slug resolution, route parser, and project filters** — create `src/projectRoute.ts` and `src/projectRoute.test.ts`:
   - Implement `CockpitProject`, `deriveProjects(connections: GitConnections | null): CockpitProject[]`, `slugify`, `projectSlugs`, `projectSlug`, and `findProjectByRef` matching AIFactory's `lib/project-route.ts` precedence (canonical slug > `id` > derived/repo alias).
   - Implement `parseCockpitRoute(pathname: string): { kind: "all"; view: View; canonical: string } | { kind: "project"; ref: string; view: View; canonicalViewSeg: string } | { kind: "redirect"; to: string }` over `NAV` views from `src/dashboard.ts:19-49` (redirecting `/projects` -> `/` and unknown view segments to the base path).
   - Implement `buildCockpitPath(slug: string | null, view: View): string` (`/` or `/<view>` when `slug === null`; `/p/<encodedSlug>` or `/p/<encodedSlug>/<view>` when `slug` is set and `view !== "overview"`).
   - Implement `filterCardsForProject(cards: Card[], project: CockpitProject | null, defaultRepoId: number | null | undefined): Card[]` and `filterWorkItemsForProject(items: WorkItem[], project: CockpitProject | null, projectCards: Card[]): WorkItem[]`.
   - **Check:** `(cd apps/frontend-web && npx vitest run src/projectRoute.test.ts && npm run typecheck && npm run lint)`
   - **Traps:** Repo runs `exactOptionalPropertyTypes` and `noUncheckedIndexedAccess` via `standards/tsconfig.base.json`; never assign `undefined` to optional props without `| undefined`, and guard array indexing.

2. **(agy) `ProjectBreadcrumb`, slug-keeping `PortalSwitcher`, and `CommandPalette` project switcher** — create `src/ProjectBreadcrumb.tsx` and `src/ProjectBreadcrumb.test.tsx`; edit `src/PortalSwitcher.tsx:10-43`, `src/PortalSwitcher.test.tsx:1-24`, `src/CommandPalette.tsx:20-229`, `src/CommandPalette.test.tsx:1-47`, and `src/index.css:152-182`:
   - In `src/ProjectBreadcrumb.tsx`, implement `BreadcrumbProject` and `ProjectBreadcrumb` with the exact props and a11y semantics of `Factory/shared/factory-ui/ProjectBreadcrumb.tsx` (`aria-haspopup="listbox"`, `aria-expanded`, search `role="combobox"`, `aria-activedescendant`, `ArrowDown`/`ArrowUp`/`Enter`/`Escape`, outside `mousedown`, trigger focus restore, `onViewAll` rendering `"All projects..."`, and trigger fallback `"All projects"` when `currentSlug` is undefined).
   - In `src/PortalSwitcher.tsx:10-43`, add optional `project?: { slug: string; portals?: string[] } | undefined` and build sibling `href` as `${p.url.replace(/\/+$/, "")}/p/${encodeURIComponent(project.slug)}` when `project?.slug` is set and `project.portals ?? ["plan", "build", "test"]` includes `p.key`.
   - In `src/CommandPalette.tsx:38-116`, add optional `projects?: { slug: string; name: string }[] | undefined` and `onSwitchProject?: ((slug: string | null) => void) | undefined`; add the `"Switch project..."` action (entering `projectMode` and refocusing the input, plus an `"All projects"` row at the top of project mode).
   - In `src/index.css:162`, add compact `.proj-bc` rules using existing `--panel`, `--panel-2`, `--border`, `--text`, `--muted`, `--cyan` tokens.
   - **Check:** `(cd apps/frontend-web && npx vitest run src/ProjectBreadcrumb.test.tsx src/PortalSwitcher.test.tsx src/CommandPalette.test.tsx && npm run typecheck && npm run lint)`
   - **Traps:** `src/index.css` is hand-authored — NEVER run `prettier --write` on `src/index.css`. Frontend tests run in node via `renderToStaticMarkup` (no `@testing-library/react` is installed in `package.json`); test state transitions via pure helpers or React test renderer hooks without adding new npm packages. No emojis.

3. **(agy) Wire URL routing, project-scoped filtering, and topbar breadcrumb into the cockpit** — edit `src/useDashboard.ts:29-57,147-151,243-266`, `src/cards.ts:264-281,382-431`, `src/BacklogView.tsx:63-78`, `src/PlanningBoard.tsx:20-35`, `src/App.tsx:30-289`, and add routing tests in `src/projectRoute.test.ts`:
   - In `src/useDashboard.ts:54-75`, initialise `view` and `projectRef` from `parseCockpitRoute(window.location.pathname)`, listen to `window.addEventListener("popstate", ...)`, fetch tenant git connections + cards alongside work items so project resolution and work-item correlation-key joining are available at the shell, and make `setView(nextView)` / `setProjectSlug(nextSlug)` push `/p/:slug[/:view]` or `/[view]` via `window.history.pushState`.
   - In `src/useDashboard.ts`, when `route.kind === "redirect"` or `urlProject` resolves via ID/alias rather than canonical slug, call `window.history.replaceState` to canonicalize the URL without adding a history entry.
   - In `src/App.tsx:229-289`, render `<ProjectBreadcrumb projects={breadcrumbProjects} currentSlug={activeProjectSlug} onSelect={setProjectSlug} onViewAll={() => setProjectSlug(null)} />` in `.topbar` right after `<PortalSwitcher needsCount={needsCount} project={activeProjectSlug ? { slug: activeProjectSlug } : undefined} />`; pass `projects` and `onSwitchProject` to `<CommandPalette />`; gate `/p/:ref` content on `connectionsLoaded`, rendering the `"Project not found"` panel with an `"All projects"` button when `projectRef` is unknown, and passing `project={activeProject}` (or filtered `items`) to the active view, `PipelineStrip`, and `EventTicker`.
   - In `src/BacklogView.tsx:63-78` and `src/PlanningBoard.tsx:20-35`, accept optional `project?: CockpitProject | null | undefined` and `defaultRepositoryId?: number | null | undefined`, filtering `cards` via `filterCardsForProject` before query/priority sort.
   - **Check:** `(cd apps/frontend-web && npm test && npm run typecheck && npm run lint && npm run format:check && npm run build) && python3 /mnt/code/Source-home/GitHub/Factory/scripts/check_factory_ui_drift.py --self-test`
   - **Traps:** Preserve `/?task=<key>` deep-link stripping in `src/App.tsx:86-92` so `window.location.pathname` (`/p/:slug`) is kept when `?task=` is removed. Do not exceed `--max-warnings 239` in `npm run lint`.

## Tests

```bash
cd apps/frontend-web
npm test
npm run typecheck
npm run lint
npm run format:check
npm run build
python3 /mnt/code/Source-home/GitHub/Factory/scripts/check_factory_ui_drift.py --self-test
```

## Rollback

Revert the frontend commits on the feature branch (or revert the PR merge on `dev`). Because no backend routes, database schemas, or persistent browser storage keys are modified, rollback is stateless and immediate.

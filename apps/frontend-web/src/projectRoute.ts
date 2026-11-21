import type { Card, GitConnections, WorkItem } from "./api";
import { NAV, type View } from "./dashboard";

export type CockpitProject = {
  id: string;
  name: string;
  slug?: string;
  createdAt: string;
  repositoryIds: number[];
  repoPaths: string[];
};

export function deriveProjects(connections: GitConnections | null): CockpitProject[] {
  const projects = new Map<string, CockpitProject>();
  for (const connection of connections?.connections ?? []) {
    for (const repo of connection.repositories) {
      const id = repo.aifactory_project_id ? repo.aifactory_project_id : `repo:${String(repo.id)}`;
      const existing = projects.get(id);
      if (existing) {
        existing.repositoryIds.push(repo.id);
        existing.repoPaths.push(repo.project);
        if (repo.created_at && (!existing.createdAt || repo.created_at < existing.createdAt)) {
          existing.createdAt = repo.created_at;
        }
      } else {
        projects.set(id, {
          id,
          name: repo.aifactory_project_id
            ? (repo.project.split("/").pop() ?? repo.project)
            : repo.project,
          createdAt: repo.created_at ?? "",
          repositoryIds: [repo.id],
          repoPaths: [repo.project],
        });
      }
    }
  }
  const result = [...projects.values()];
  const names = result.map((project) => project.name);
  for (const project of result) {
    if (
      !project.id.startsWith("repo:") &&
      result.some(
        (other, index) =>
          other !== project &&
          names[index] === project.name &&
          other.repoPaths[0] !== project.repoPaths[0],
      )
    ) {
      project.name = project.repoPaths[0] ?? project.name;
    }
  }
  return result;
}

export function slugify(name: string, fallback: string): string {
  return (
    name
      .normalize("NFKD")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || fallback
  );
}

const created = (project: CockpitProject) => new Date(project.createdAt).getTime() || 0;

export function projectSlugs(projects: CockpitProject[]): Map<string, string> {
  const out = new Map<string, string>();
  const used = new Set<string>();
  for (const project of projects) {
    if (project.slug) {
      out.set(project.id, project.slug);
      used.add(project.slug);
    }
  }
  for (const project of projects
    .filter((p) => !p.slug)
    .sort((a, b) => created(a) - created(b) || a.id.localeCompare(b.id))) {
    const base = slugify(project.name, project.id);
    const withId = `${base}-${project.id.slice(0, 8)}`;
    let slug = used.has(base) ? withId : base;
    for (let n = 2; used.has(slug); n++) slug = `${withId}-${String(n)}`;
    used.add(slug);
    out.set(project.id, slug);
  }
  return out;
}

export const projectSlug = (projects: CockpitProject[], project: CockpitProject): string =>
  projectSlugs(projects).get(project.id) ?? project.id;

export function findProjectByRef(
  projects: CockpitProject[],
  ref: string,
): CockpitProject | undefined {
  const lower = ref.toLowerCase();
  const slugs = projectSlugs(projects);
  return (
    projects.find((p) => slugs.get(p.id)?.toLowerCase() === lower) ??
    projects.find((p) => p.id.toLowerCase() === lower) ??
    projects.find(
      (p) =>
        p.slug?.toLowerCase() === lower ||
        slugify(p.name, p.id) === lower ||
        `${slugify(p.name, p.id)}-${p.id.slice(0, 8)}`.toLowerCase() === lower ||
        p.repoPaths.some((path) => slugify(path, p.id) === lower),
    )
  );
}

export type CockpitRoute =
  | { kind: "all"; view: View; canonical: string }
  | { kind: "project"; ref: string; view: View; canonicalViewSeg: string }
  | { kind: "redirect"; to: string };

const viewFor = (segment: string): View | undefined =>
  NAV.find((entry) => entry.id === segment)?.id;

export function parseCockpitRoute(pathname: string): CockpitRoute {
  if (pathname === "/projects" || pathname === "/projects/") return { kind: "redirect", to: "/" };
  const parts = pathname.split("/");
  if (parts[1] === "p" && parts[2]) {
    let ref = parts[2];
    try {
      ref = decodeURIComponent(ref);
    } catch {
      /* preserve the raw reference */
    }
    const base = `/p/${encodeURIComponent(ref)}`;
    const segment = parts[3];
    if (parts.length > 4 || (segment && !viewFor(segment))) return { kind: "redirect", to: base };
    const view = segment && segment !== "overview" ? viewFor(segment) : "overview";
    if (!view || segment === "overview" || pathname !== buildCockpitPath(ref, view)) {
      return { kind: "redirect", to: buildCockpitPath(ref, view ?? "overview") };
    }
    return { kind: "project", ref, view, canonicalViewSeg: view === "overview" ? "" : `/${view}` };
  }
  if (pathname === "/") return { kind: "all", view: "overview", canonical: "/" };
  const view = parts.length === 2 ? viewFor(parts[1] ?? "") : undefined;
  if (view && view !== "overview") return { kind: "all", view, canonical: `/${view}` };
  return { kind: "redirect", to: "/" };
}

export function buildCockpitPath(slug: string | null, view: View): string {
  const base = slug === null ? "" : `/p/${encodeURIComponent(slug)}`;
  return `${base}${view === "overview" ? "" : `/${view}`}` || "/";
}

export function filterCardsForProject(
  cards: Card[],
  project: CockpitProject | null,
  defaultRepoId: number | null | undefined,
): Card[] {
  if (!project) return cards;
  return cards.filter((card) =>
    project.repositoryIds.includes(card.repository_id ?? defaultRepoId ?? -1),
  );
}

export function filterWorkItemsForProject(
  items: WorkItem[],
  project: CockpitProject | null,
  projectCards: Card[],
): WorkItem[] {
  if (!project) return items;
  const paths = new Set(project.repoPaths.map((path) => path.toLowerCase()));
  const keys = new Set(
    projectCards.map((card) => card.correlation_key).filter((key): key is string => key !== null),
  );
  return items.filter(
    (item) =>
      keys.has(item.correlation_key) ||
      [item.pfactory.repo, item.aifactory.repo, item.tfactory.repo].some(
        (repo) => repo && paths.has(repo.toLowerCase()),
      ),
  );
}

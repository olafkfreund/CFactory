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
    for (const repo of [...connection.repositories].sort((a, b) =>
      (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.id - b.id,
    )) {
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
  for (const project of result) {
    const primary = project.repositoryIds
      .map((id, index) => ({ id, path: project.repoPaths[index] ?? "" }))
      .sort((a, b) => a.id - b.id)[0];
    if (primary) project.name = project.id.startsWith("repo:") ? primary.path :
      (primary.path.split("/").pop() ?? primary.path);
    project.repositoryIds.sort((a, b) => a - b);
    project.repoPaths.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  }
  const names = result.map((project) => project.name);
  for (const project of result) {
    if (
      !project.id.startsWith("repo:") &&
      result.some(
        (other, index) =>
          other !== project &&
          names[index]?.toLowerCase() === project.name.toLowerCase() &&
          other.repoPaths[0] !== project.repoPaths[0],
      )
    ) {
      project.name = project.repoPaths[0] ?? project.name;
    }
  }
  return result.sort((a, b) => (a.createdAt || "9999").localeCompare(b.createdAt || "9999") || a.id.localeCompare(b.id));
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
  const bases = projects.map((project) => slugify(project.name, slugify(project.id, "project")));
  for (const project of projects) {
    if (project.slug) {
      out.set(project.id, project.slug);
      used.add(project.slug.toLowerCase());
    }
  }
  for (const project of projects
    .filter((p) => !p.slug)
    .sort((a, b) => created(a) - created(b) || a.id.localeCompare(b.id))) {
    const base = slugify(project.name, slugify(project.id, "project"));
    const withId = `${base}-${slugify(project.id, "project").slice(0, 8)}`;
    let slug = used.has(base) || bases.filter((candidate) => candidate === base).length > 1 ? withId : base;
    for (let n = 2; used.has(slug.toLowerCase()); n++) slug = `${withId}-${String(n)}`;
    used.add(slug.toLowerCase());
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
  const canonical = projects.filter((p) => slugs.get(p.id)?.toLowerCase() === lower);
  if (canonical.length) return canonical.length === 1 ? canonical[0] : undefined;
  const ids = projects.filter((p) => p.id.toLowerCase() === lower);
  if (ids.length) return ids.length === 1 ? ids[0] : undefined;
  const aliases = projects.filter(
      (p) =>
        p.slug?.toLowerCase() === lower ||
        slugify(p.name, p.id) === lower ||
        `${slugify(p.name, p.id)}-${slugify(p.id, "project").slice(0, 8)}` === lower ||
        p.repoPaths.some((path) => slugify(path, p.id) === lower),
  );
  return aliases.length === 1 ? aliases[0] : undefined;
}

export type CockpitRoute =
  | { kind: "all"; view: View; canonical: string }
  | { kind: "project"; ref: string; view: View; canonicalViewSeg: string }
  | { kind: "redirect"; to: string };

const viewFor = (segment: string): View | undefined =>
  segment === "preferences" ? "preferences" : NAV.find((entry) => entry.id === segment)?.id;

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
    if (segment && ["tokens", "audit", "services", "preferences"].includes(segment)) {
      return { kind: "redirect", to: `/${segment}` };
    }
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
  cards: Card[],
  projects: CockpitProject[],
  defaultRepoId: number | null | undefined,
): WorkItem[] {
  if (!project) return items;
  const byKey = new Map<string, number | null>();
  for (const card of cards) {
    if (!card.correlation_key) continue;
    const repoId = card.repository_id ?? defaultRepoId ?? null;
    byKey.set(card.correlation_key,
      byKey.has(card.correlation_key) && byKey.get(card.correlation_key) !== repoId ? null : repoId);
  }
  return items.filter((item) => {
    if (byKey.has(item.correlation_key)) {
      return project.repositoryIds.includes(byKey.get(item.correlation_key) ?? -1);
    }
    const paths = [item.pfactory.repo, item.aifactory.repo, item.tfactory.repo].filter(Boolean);
    const owners = projects.filter((candidate) => paths.some((repo) =>
      candidate.repoPaths.some((path) => path.toLowerCase() === repo?.toLowerCase()),
    ));
    return owners.length === 1 && owners[0]?.id === project.id;
  });
}

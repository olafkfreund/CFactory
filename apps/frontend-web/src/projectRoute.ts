import type { Card, WorkItem } from "./api";
import { NAV, type View } from "./dashboard";

export type CockpitProject = {
  id: string;
  name: string;
  slug?: string;
  createdAt: string;
  repositoryIds: number[];
  repoPaths: string[];
};

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
    byKey.set(
      card.correlation_key,
      byKey.has(card.correlation_key) && byKey.get(card.correlation_key) !== repoId ? null : repoId,
    );
  }
  return items.filter((item) => {
    if (byKey.has(item.correlation_key)) {
      return project.repositoryIds.includes(byKey.get(item.correlation_key) ?? -1);
    }
    const paths = [item.pfactory.repo, item.aifactory.repo, item.tfactory.repo].filter(Boolean);
    const owners = projects.filter((candidate) =>
      paths.some((repo) =>
        candidate.repoPaths.some((path) => path.toLowerCase() === repo?.toLowerCase()),
      ),
    );
    return owners.length === 1 && owners[0]?.id === project.id;
  });
}

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { deriveProjects, findProjectByRef, projectSlugs, slugify } from "./projectRoute";
import type { CockpitProject } from "./projectRoute";

type FixtureProject = {
  id: string; name: string; slug?: string; created_at: string; server_canonical?: boolean;
  repository_ids: number[]; repo_paths: string[];
};
type Fixture = {
  repositories: ({ id: number; project: string; aifactory_project_id: string | null; created_at: string } & Record<string, unknown>)[];
  expected_projects: FixtureProject[];
  extra_projects: FixtureProject[];
  expected_slugs: Record<string, string>;
  references: { ref: string; id: string | null }[];
};

describe("project derivation parity fixture", () => {
  it("matches the shared project, slug, and reference cases", () => {
    // read at run time: the fixture lives outside this package, which the frontend Docker build copies alone
    const fixture = JSON.parse(readFileSync(new URL("../../../tests/data/projects.json", import.meta.url), "utf8")) as Fixture;
    // The portal differs on ICU path order; PR B makes the server list canonical.
    const canonicalIds = new Set(fixture.expected_projects.filter((p) => "server_canonical" in p).map((p) => p.id));
    const repositories = fixture.repositories.filter((repo) => !canonicalIds.has(repo.aifactory_project_id ?? "")).map((repo) => ({
      ...repo,
      connection_id: 1,
      tenant_id: "default",
    }));
    const derived = deriveProjects({
      connections: [{ id: 1, tenant_id: "default", provider: "github", base_url: "https://github.com", label: "github", status: "configured", repositories }],
      default_repository_id: 1,
    } as Parameters<typeof deriveProjects>[0]);
    const expected = fixture.expected_projects.filter((p) => !canonicalIds.has(p.id)).map((p) => ({
      id: p.id, name: p.name, createdAt: p.created_at,
      repositoryIds: p.repository_ids, repoPaths: p.repo_paths,
    }));
    expect(derived).toEqual(expected);
    const all = [...derived, ...fixture.extra_projects.map((p) => ({
      id: p.id, name: p.name, ...(p.slug ? { slug: p.slug } : {}), createdAt: p.created_at,
      repositoryIds: p.repository_ids, repoPaths: p.repo_paths,
    } satisfies CockpitProject))];
    expect(Object.fromEntries(projectSlugs(all))).toEqual(Object.fromEntries(
      Object.entries(fixture.expected_slugs).filter(([id]) => !canonicalIds.has(id)),
    ));
    for (const { ref, id } of fixture.references) expect(findProjectByRef(all, ref)?.id ?? null).toBe(id);
    expect(findProjectByRef(derived, "demo")).toBeUndefined();
    expect(slugify("Café / Widgets", "fallback")).toBe("cafe-widgets");
    expect(slugify("---", "fallback")).toBe("fallback");
  });
});

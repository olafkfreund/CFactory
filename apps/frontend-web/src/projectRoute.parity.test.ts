import { describe, expect, it } from "vitest";
import fixture from "../../../tests/data/projects.json";
import { deriveProjects, findProjectByRef, projectSlugs, slugify } from "./projectRoute";
import type { CockpitProject } from "./projectRoute";

describe("project derivation parity fixture", () => {
  it("matches the shared project, slug, and reference cases", () => {
    const repositories = fixture.repositories.map((repo) => ({
      ...repo,
      connection_id: 1,
      tenant_id: "default",
    }));
    const derived = deriveProjects({
      connections: [{ id: 1, tenant_id: "default", provider: "github", base_url: "https://github.com", label: "github", status: "configured", repositories }],
      default_repository_id: 1,
    } as Parameters<typeof deriveProjects>[0]);
    const expected = fixture.expected_projects.map((p) => ({
      id: p.id, name: p.name, createdAt: p.created_at,
      repositoryIds: p.repository_ids, repoPaths: p.repo_paths,
    }));
    expect(derived).toEqual(expected);
    const all = [...derived, ...fixture.extra_projects.map((p) => ({
      id: p.id, name: p.name, slug: p.slug, createdAt: p.created_at,
      repositoryIds: p.repository_ids, repoPaths: p.repo_paths,
    } satisfies CockpitProject))];
    expect(Object.fromEntries(projectSlugs(all))).toEqual(fixture.expected_slugs);
    for (const { ref, id } of fixture.references) expect(findProjectByRef(all, ref)?.id ?? null).toBe(id);
    expect(findProjectByRef(derived, "demo")).toBeUndefined();
    expect(slugify("Café / Widgets", "fallback")).toBe("cafe-widgets");
    expect(slugify("---", "fallback")).toBe("fallback");
  });
});

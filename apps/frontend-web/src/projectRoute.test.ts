import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { CardSchema, GitConnectionsSchema, WorkItemSchema } from "./api";
import BacklogView from "./BacklogView";
import PlanningBoard from "./PlanningBoard";
import type { CockpitProject } from "./projectRoute";
import {
  buildCockpitPath,
  deriveProjects,
  filterCardsForProject,
  filterWorkItemsForProject,
  findProjectByRef,
  parseCockpitRoute,
  projectSlug,
  projectSlugs,
  slugify,
} from "./projectRoute";
import { initialRouteState } from "./useDashboard";

const project = (id: string, name: string, createdAt = "2026-01-01"): CockpitProject => ({
  id,
  name,
  createdAt,
  repositoryIds: [1],
  repoPaths: ["acme/widgets"],
});

describe("project routes", () => {
  it("derives grouped and fallback projects from git connections", () => {
    const connections = GitConnectionsSchema.parse({
      connections: [
        {
          id: 1,
          tenant_id: "default",
          provider: "github",
          base_url: "https://github.com",
          label: "github",
          status: "configured",
          repositories: [
            {
              id: 1,
              connection_id: 1,
              tenant_id: "default",
              project: "acme/widgets",
              aifactory_project_id: "uuid-a",
              created_at: "2026-01-02",
            },
            {
              id: 2,
              connection_id: 1,
              tenant_id: "default",
              project: "acme/other",
              aifactory_project_id: "uuid-a",
              created_at: "2026-01-01",
            },
            {
              id: 3,
              connection_id: 1,
              tenant_id: "default",
              project: "else/widgets",
              aifactory_project_id: "uuid-b",
            },
            { id: 4, connection_id: 1, tenant_id: "default", project: "acme/free" },
          ],
        },
      ],
    });
    const projects = deriveProjects(connections);
    expect(projects).toHaveLength(3);
    expect(projects[0]).toMatchObject({
      id: "uuid-a",
      name: "acme/widgets",
      createdAt: "2026-01-01",
      repositoryIds: [1, 2],
      repoPaths: ["acme/widgets", "acme/other"],
    });
    expect(projects[1]?.name).toBe("else/widgets");
    expect(projects[2]).toMatchObject({ id: "repo:4", name: "acme/free" });
    expect(deriveProjects(null)).toEqual([]);
  });

  it("uses the reference slug order and resolves canonical slugs before IDs and aliases", () => {
    const older = project("abcdefgh-old", "Demo", "2025-01-01");
    const newer = project("abcdefgh-new", "Demo", "2026-01-01");
    const namedLikeId = project("demo", "Other");
    const projects = [newer, older, namedLikeId];
    expect(slugify("Café / Widgets", "fallback")).toBe("cafe-widgets");
    expect(slugify("---", "fallback")).toBe("fallback");
    expect(projectSlugs(projects).get(older.id)).toBe("demo");
    expect(projectSlugs(projects).get(newer.id)).toBe("demo-abcdefgh");
    expect(projectSlug(projects, older)).toBe("demo");
    expect(findProjectByRef(projects, "demo")).toBe(older);
    expect(findProjectByRef(projects, "ABCDEFGH-NEW")).toBe(newer);
    expect(findProjectByRef(projects, "acme-widgets")).toBe(newer);
  });

  it("parses and builds cross-project and project paths", () => {
    expect(parseCockpitRoute("/")).toEqual({ kind: "all", view: "overview", canonical: "/" });
    expect(parseCockpitRoute("/planning")).toEqual({
      kind: "all",
      view: "planning",
      canonical: "/planning",
    });
    expect(parseCockpitRoute("/projects")).toEqual({ kind: "redirect", to: "/" });
    expect(parseCockpitRoute("/unknown")).toEqual({ kind: "redirect", to: "/" });
    expect(parseCockpitRoute("/p/my%20project")).toEqual({
      kind: "project",
      ref: "my project",
      view: "overview",
      canonicalViewSeg: "",
    });
    expect(parseCockpitRoute("/p/widgets/planning")).toEqual({
      kind: "project",
      ref: "widgets",
      view: "planning",
      canonicalViewSeg: "/planning",
    });
    expect(parseCockpitRoute("/p/widgets/overview")).toEqual({
      kind: "redirect",
      to: "/p/widgets",
    });
    expect(parseCockpitRoute("/p/widgets/nope")).toEqual({ kind: "redirect", to: "/p/widgets" });
    expect(buildCockpitPath(null, "overview")).toBe("/");
    expect(buildCockpitPath(null, "needs")).toBe("/needs");
    expect(buildCockpitPath("my project", "planning")).toBe("/p/my%20project/planning");
  });
});

describe("project filters", () => {
  const active = project("uuid", "Widgets");
  const cards = [
    CardSchema.parse({
      card_key: "one",
      tenant_id: "default",
      title: "One",
      acceptance_criteria: [],
      status: "ready",
      priority: 1,
      tier: null,
      assignee: null,
      milestone: null,
      correlation_key: "task-one",
      repository_id: 1,
      created_at: "2026-01-01",
      updated_at: "2026-01-01",
    }),
    CardSchema.parse({
      card_key: "two",
      tenant_id: "default",
      title: "Two",
      acceptance_criteria: [],
      status: "ready",
      priority: 1,
      tier: null,
      assignee: null,
      milestone: null,
      correlation_key: "task-two",
      repository_id: 2,
      created_at: "2026-01-01",
      updated_at: "2026-01-01",
    }),
    CardSchema.parse({
      card_key: "default",
      tenant_id: "default",
      title: "Default",
      acceptance_criteria: [],
      status: "ready",
      priority: 1,
      tier: null,
      assignee: null,
      milestone: null,
      correlation_key: "task-default",
      created_at: "2026-01-01",
      updated_at: "2026-01-01",
    }),
  ];
  const item = (key: string, repo: string | null = null) =>
    WorkItemSchema.parse({
      correlation_key: key,
      title: null,
      timeline: [],
      pfactory: { task_id: null, status: null, phase: null, repo },
      aifactory: { task_id: null, status: null, phase: null },
      tfactory: { task_id: null, status: null, phase: null },
    });

  it("matches effective card repository and service repo or card correlation", () => {
    expect(filterCardsForProject(cards, active, 1).map((card) => card.card_key)).toEqual([
      "one",
      "default",
    ]);
    expect(filterCardsForProject(cards, active, null).map((card) => card.card_key)).toEqual([
      "one",
    ]);
    expect(filterCardsForProject(cards, null, 1)).toBe(cards);
    const items = [
      item("task-one"),
      item("task-two"),
      item("external", "ACME/WIDGETS"),
      item("unmatched"),
    ];
    expect(
      filterWorkItemsForProject(items, active, cards.slice(0, 1)).map(
        (work) => work.correlation_key,
      ),
    ).toEqual(["task-one", "external"]);
    expect(filterWorkItemsForProject(items, null, [])).toBe(items);
  });

  it("initialises route state, rewrites redirects/aliases, and renders project-scoped planning views", () => {
    expect(initialRouteState("/")).toEqual({
      view: "overview",
      projectRef: null,
      redirectTo: null,
    });
    expect(initialRouteState("/projects")).toEqual({
      view: "overview",
      projectRef: null,
      redirectTo: "/",
    });
    expect(initialRouteState("/p/uuid/planning")).toEqual({
      view: "planning",
      projectRef: "uuid",
      redirectTo: null,
    });
    expect(initialRouteState("/p/uuid/unknown")).toEqual({
      view: "overview",
      projectRef: "uuid",
      redirectTo: "/p/uuid",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ cards }), { status: 200 }))),
    );
    const backlogHtml = renderToStaticMarkup(
      createElement(BacklogView, {
        reloadSignal: 0,
        project: active,
        defaultRepositoryId: 1,
      }),
    );
    expect(backlogHtml).toContain("Backlog");

    const planningHtml = renderToStaticMarkup(
      createElement(PlanningBoard, {
        reloadSignal: 0,
        project: active,
        defaultRepositoryId: 1,
      }),
    );
    expect(planningHtml).toContain("Planning board");
    vi.unstubAllGlobals();
  });
});

import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { CardSchema, WorkItemSchema } from "./api";
import BacklogView from "./BacklogView";
import PlanningBoard from "./PlanningBoard";
import type { CockpitProject } from "./projectRoute";
import {
  buildCockpitPath,
  filterCardsForProject,
  filterWorkItemsForProject,
  parseCockpitRoute,
} from "./projectRoute";
import { initialRouteState } from "./useDashboard";

const project = (id: string, name: string, createdAt = "2026-01-01"): CockpitProject => ({
  id,
  name,
  slug: name.toLowerCase(),
  createdAt,
  repositoryIds: [1],
  repoPaths: ["acme/widgets"],
});

describe("project routes", () => {
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
      filterWorkItemsForProject(items, active, cards, [active], 1).map(
        (work) => work.correlation_key,
      ),
    ).toEqual(["task-one", "external"]);
    expect(filterWorkItemsForProject(items, null, [], [], null)).toBe(items);
    const beta = { ...active, id: "beta", repositoryIds: [2], repoPaths: ["acme/other"] };
    const conflicting = [item("task-two", "acme/widgets"), item("unknown", "acme/widgets")];
    expect(
      filterWorkItemsForProject(conflicting, active, cards, [active, beta], 1).map(
        (w) => w.correlation_key,
      ),
    ).toEqual(["unknown"]);
    expect(
      filterWorkItemsForProject(conflicting, beta, cards, [active, beta], 1).map(
        (w) => w.correlation_key,
      ),
    ).toEqual(["task-two"]);
    expect(
      filterWorkItemsForProject(
        [item("unknown", "acme/widgets")],
        active,
        [],
        [active, { ...beta, repoPaths: ["acme/widgets"] }],
        1,
      ),
    ).toEqual([]);
    expect(
      filterWorkItemsForProject(
        [item("task-one", "acme/widgets")],
        active,
        [cards[0]!, { ...cards[0]!, repository_id: 2 }],
        [active, beta],
        1,
      ),
    ).toEqual([]);
  });

  it("initialises route state and renders project-scoped planning views", () => {
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

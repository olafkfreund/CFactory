import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { JSDOM } from "jsdom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CardSchema, GitConnectionsSchema, WorkItemSchema } from "./api";
import type { Card, GitConnections, ServerProject, WorkItem } from "./api";
import { useDashboard } from "./useDashboard";
import ProjectBreadcrumb from "./ProjectBreadcrumb";
import PortalSwitcher from "./PortalSwitcher";
import BacklogView from "./BacklogView";
import PlanningBoard from "./PlanningBoard";
import CommandPalette from "./CommandPalette";
import App from "./App";

const api = vi.hoisted(() => ({
  fetchWorkItems: vi.fn(),
  fetchCards: vi.fn(),
  patchCard: vi.fn(),
  fetchSettings: vi.fn(),
  fetchGitConnections: vi.fn(),
  fetchProjects: vi.fn(),
  resolveProject: vi.fn(),
  fetchProgress: vi.fn(),
  fetchHealth: vi.fn(),
  fetchCardSyncState: vi.fn(),
  createCard: vi.fn(),
  searchWorkItems: vi.fn(),
  openFeed: vi.fn(),
  refresh: vi.fn(),
  fetchLiveAgents: vi.fn(),
  proposeAction: vi.fn(),
}));
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  ...api,
}));
vi.mock("./LiveAgents", () => ({ default: ({ onOpenConsole }: { onOpenConsole?: (agent: unknown) => void }) => <button onClick={() => onOpenConsole?.({ correlation_key: "alpha-task", ws_path: "/console", title: "Alpha agent", phase: null })}>Live agent console</button>, AgentTerminal: () => null }));

const connections: GitConnections = GitConnectionsSchema.parse({
  default_repository_id: 1,
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
          project: "acme/alpha",
          aifactory_project_id: "alpha",
        },
        {
          id: 2,
          connection_id: 1,
          tenant_id: "default",
          project: "acme/beta",
          aifactory_project_id: "beta",
        },
      ],
    },
  ],
});
const serverProjects: ServerProject[] = [
  {
    id: "alpha",
    name: "alpha",
    slug: "alpha",
    repository_ids: [1],
    repo_paths: ["acme/alpha"],
    created_at: "",
  },
  {
    id: "beta",
    name: "beta",
    slug: "beta",
    repository_ids: [2],
    repo_paths: ["acme/beta"],
    created_at: "",
  },
];
const card = (key: string, repo: number): Card =>
  CardSchema.parse({
    card_key: key,
    tenant_id: "default",
    title: key,
    acceptance_criteria: [],
    status: "ready",
    priority: 1,
    tier: null,
    assignee: null,
    milestone: null,
    correlation_key: key,
    repository_id: repo,
    created_at: "2026-01-01",
    updated_at: "2026-01-01",
  });
const work = (key: string, repo: string, status: string | null = null): WorkItem =>
  WorkItemSchema.parse({
    correlation_key: key,
    title: key,
    timeline: [],
    pfactory: { task_id: null, status, phase: null, repo },
    aifactory: { task_id: null, status: null, phase: null },
    tfactory: { task_id: null, status: null, phase: null },
  });
let dom: JSDOM;
let root: Root;
let host: HTMLDivElement;
let feed: ((message: unknown) => void) | undefined;
function must<T>(value: T | null | undefined): T {
  if (value == null) throw new Error("missing test element");
  return value;
}
const mount = async (node: React.ReactNode) => {
  await act(async () => {
    root.render(node);
    await Promise.resolve();
  });
};
const click = async (node: Element) => {
  await act(async () => {
    node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await Promise.resolve();
  });
};

beforeEach(() => {
  dom = new JSDOM("<!doctype html><div id='root'></div>", { url: "https://cockpit.test/" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    Node: dom.window.Node,
    Event: dom.window.Event,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: (callback: FrameRequestCallback) => dom.window.setTimeout(() => { callback(performance.now()); }, 0),
    cancelAnimationFrame: (id: number) => { dom.window.clearTimeout(id); },
  });
  dom.window.HTMLElement.prototype.scrollIntoView = () => undefined;
  Object.assign(dom.window.HTMLElement.prototype, {
    attachEvent: () => undefined,
    detachEvent: () => undefined,
  });
  host = dom.window.document.querySelector("#root") as HTMLDivElement;
  root = createRoot(host);
  feed = undefined;
  api.fetchWorkItems.mockResolvedValue([
    work("alpha-task", "acme/alpha"),
    work("beta-task", "acme/beta"),
  ]);
  api.fetchCards.mockResolvedValue([card("alpha-task", 1), card("beta-task", 2)]);
  api.patchCard.mockImplementation((key: string, patch: object) => Promise.resolve({ ...card(key, 1), ...patch }));
  api.fetchSettings.mockResolvedValue({ tenant: "default" });
  api.fetchGitConnections.mockResolvedValue(connections);
  api.fetchProjects.mockResolvedValue(serverProjects);
  api.resolveProject.mockResolvedValue({ error: "unknown" });
  api.fetchProgress.mockResolvedValue([]);
  api.fetchHealth.mockResolvedValue({ upstreams: {} });
  api.fetchCardSyncState.mockResolvedValue({
    now: "2026-01-01",
    poll: { enabled: true, interval_seconds: 300, live: true },
    repositories: [
      {
        repository_id: 1,
        project: "acme/alpha",
        is_default: true,
        last_polled_at: null,
        watermark_at: null,
        stale: true,
      },
      {
        repository_id: 2,
        project: "acme/beta",
        is_default: false,
        last_polled_at: "2026-01-01",
        watermark_at: null,
        stale: false,
      },
    ],
  });
  api.openFeed.mockImplementation((onMessage: (message: unknown) => void) => {
    feed = onMessage;
    return { close: () => undefined };
  });
  api.refresh.mockResolvedValue(undefined);
  api.fetchLiveAgents.mockResolvedValue({ rmux_enabled: true, agents: [] });
  api.createCard.mockResolvedValue(card("new", 2));
});
afterEach(async () => {
  await act(async () => {
    root.unmount();
    await Promise.resolve();
  });
  dom.window.close();
  vi.clearAllMocks();
});

function RouteProbe() {
  const d = useDashboard();
  return (
    <main>
      <span data-testid="path">
        {d.projectRef ?? "all"}:{d.view}
      </span>
      <span data-testid="items">{d.items.map((item) => item.correlation_key).join(",")}</span>
      <span data-testid="pins">{d.pins.map((pin) => pin.key).join(",")}</span>
      <span data-testid="toasts">{d.toasts.map((toast) => toast.ev.key).join(",")}</span>
      <span data-testid="alarm">{d.alarm?.stage ?? "none"}</span>
      <span data-testid="error">{d.connectionsError ?? "none"}</span>
      <button
        onClick={() => {
          d.setProjectSlug(null);
        }}
      >
        All projects
      </button>
    </main>
  );
}

describe("mounted project routes", () => {
  it("loads each project's cards and follows popstate back and forward", async () => {
    dom.window.history.replaceState({}, "", "/p/alpha/planning");
    await mount(<RouteProbe />);
    expect(host.querySelector('[data-testid="items"]')?.textContent).toBe("alpha-task");
    dom.window.history.pushState({}, "", "/p/beta/planning");
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.PopStateEvent("popstate"));
      await Promise.resolve();
    });
    expect(host.querySelector('[data-testid="items"]')?.textContent).toBe("beta-task");
    dom.window.history.replaceState({}, "", "/p/alpha/planning");
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.PopStateEvent("popstate"));
      await Promise.resolve();
    });
    expect(host.querySelector('[data-testid="items"]')?.textContent).toBe("alpha-task");
  });

  it("moves fleet-wide tabs out of project URLs", async () => {
    dom.window.history.replaceState({}, "", "/p/alpha/tokens");
    await mount(<RouteProbe />);
    expect(dom.window.location.pathname).toBe("/tokens");
    expect(host.querySelector('[data-testid="path"]')?.textContent).toBe("all:tokens");
  });

  it("keeps project settings scoped and routes preferences separately", async () => {
    dom.window.history.replaceState({}, "", "/p/alpha/settings");
    await mount(<App />);
    expect(dom.window.location.pathname).toBe("/p/alpha/settings");
    expect(host.textContent).toContain("Project Settings");
    expect(host.textContent).toContain("acme/alpha");
    expect(host.textContent).not.toContain("Copilot model");
    dom.window.history.pushState({}, "", "/preferences");
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.PopStateEvent("popstate"));
      await Promise.resolve();
    });
    expect(host.querySelector("h1")?.textContent).toBe("Preferences");
    expect(host.querySelector('[aria-label="Colour theme"]')).not.toBeNull();
  });

  it("keeps another project's live failure out of pins and alarm", async () => {
    dom.window.history.replaceState({}, "", "/p/alpha");
    await mount(<RouteProbe />);
    await act(async () => {
      feed?.({ type: "workitem", item: work("beta-task", "acme/beta", "failed") });
      await Promise.resolve();
    });
    expect(host.querySelector('[data-testid="pins"]')?.textContent).toBe("");
    expect(host.querySelector('[data-testid="toasts"]')?.textContent).toBe("");
    expect(host.querySelector('[data-testid="alarm"]')?.textContent).toBe("none");
  });

  it("keeps card ownership when a stage reports a different repository", async () => {
    api.fetchWorkItems.mockResolvedValueOnce([
      work("alpha-task", "acme/alpha"),
      work("beta-task", "acme/alpha"),
    ]);
    dom.window.history.replaceState({}, "", "/p/alpha");
    await mount(<RouteProbe />);
    expect(host.querySelector('[data-testid="items"]')?.textContent).toBe("alpha-task");
    dom.window.history.pushState({}, "", "/p/beta");
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.PopStateEvent("popstate"));
      await Promise.resolve();
    });
    expect(host.querySelector('[data-testid="items"]')?.textContent).toBe("beta-task");
  });

  it("exposes project fetch failure and retries", async () => {
    api.fetchProjects.mockRejectedValueOnce(new Error("projects unavailable"));
    api.refresh.mockRejectedValueOnce(new Error("refresh unavailable"));
    dom.window.history.replaceState({}, "", "/p/alpha");
    await mount(<App />);
    expect(host.textContent).toContain("Could not load projects");
    expect(host.textContent).not.toContain("Choose a project");
    await click(
      must([...host.querySelectorAll("button")].find((button) => button.textContent === "Retry")),
    );
    expect(host.textContent).not.toContain("Could not load projects");
    expect(api.refresh).not.toHaveBeenCalled();
    expect(host.querySelector('a[href*="/p/"]')).not.toBeNull();
  });

  it("offers project choices for an ambiguous alias without linking it to siblings", async () => {
    api.fetchProjects.mockResolvedValueOnce([
      {
        id: "alpha",
        name: "shared",
        slug: "shared-alpha",
        repository_ids: [1],
        repo_paths: ["acme/shared"],
        created_at: "",
      },
      {
        id: "beta",
        name: "shared",
        slug: "shared-beta",
        repository_ids: [2],
        repo_paths: ["acme/shared"],
        created_at: "",
      },
    ]);
    api.resolveProject.mockResolvedValueOnce({ error: "ambiguous" });
    dom.window.history.replaceState({}, "", "/p/acme-shared");
    await mount(<App />);
    expect(api.resolveProject).toHaveBeenCalledWith("default", "acme-shared");
    expect(host.textContent).toContain("Choose a project");
    expect(host.querySelectorAll(".mc-panel button")).toHaveLength(3);
    expect(host.querySelector('a[href*="/p/"]')).toBeNull();
    await click(
      must(
        [...host.querySelectorAll("button")].find(
          (button) => button.textContent === "All projects",
        ),
      ),
    );
    expect(dom.window.location.pathname).toBe("/");
  });

  it("resolves /p/<ref> through the server and canonicalizes the URL", async () => {
    api.resolveProject.mockResolvedValueOnce({
      project: must(serverProjects[0]),
    });
    dom.window.history.replaceState({}, "", "/p/acme-alpha/planning");
    await mount(<RouteProbe />);
    expect(api.resolveProject).toHaveBeenCalledWith("default", "acme-alpha");
    expect(dom.window.location.pathname).toBe("/p/alpha/planning");
    expect(host.querySelector('[data-testid="path"]')?.textContent).toBe("alpha:planning");
    expect(host.querySelector('[data-testid="items"]')?.textContent).toBe("alpha-task");
  });

  it("links Meet to <meet url>/p/<slug> for a grouped project and plain Meet URL for a repo: project", async () => {
    api.fetchProjects.mockResolvedValue([
      ...serverProjects,
      {
        id: "repo:3",
        name: "acme/free",
        slug: "acme-free",
        repository_ids: [3],
        repo_paths: ["acme/free"],
        created_at: "",
      },
    ]);
    dom.window.history.replaceState({}, "", "/p/alpha");
    await mount(<App />);
    const groupedMeet = must(
      [...host.querySelectorAll<HTMLAnchorElement>(".f-portal-switcher__item")].find((el) =>
        el.textContent.includes("Meet"),
      ),
    );
    expect(groupedMeet.getAttribute("href")).toBe("https://rfactory.freundcloud.org.uk/p/alpha");

    dom.window.history.pushState({}, "", "/p/acme-free");
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.PopStateEvent("popstate"));
      await Promise.resolve();
    });
    const repoMeet = must(
      [...host.querySelectorAll<HTMLAnchorElement>(".f-portal-switcher__item")].find((el) =>
        el.textContent.includes("Meet"),
      ),
    );
    expect(repoMeet.getAttribute("href")).toBe("https://rfactory.freundcloud.org.uk");
  });
});

describe("mounted project controls", () => {
  it("switches projects by keyboard and keeps the fleet entry available", async () => {
    await mount(<App />);
    const trigger = must(host.querySelector(".f-project-switcher__trigger"));
    await click(trigger);
    const input = must(host.querySelector(".f-project-switcher__input"));
    await act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await Promise.resolve();
    });
    expect(dom.window.location.pathname).toMatch(/^\/p\//);
    await click(must(host.querySelector(".f-project-switcher__trigger")));
    await click(must([...host.querySelectorAll(".f-project-switcher__action")].find((button) => button.textContent === "All projects")));
    expect(dom.window.location.pathname).toBe("/");
  });

  it("opens navigation by Ctrl+1 through Ctrl+9, search by Ctrl+K, and fleet settings", async () => {
    api.fetchSettings.mockResolvedValue({ tenant: "default", provider: "claude", model: "claude-opus-5-5", providers: ["claude"] });
    await mount(<App />);
    const paths = ["/", "/needs", "/backlog", "/planning", "/pipeline", "/running", "/tokens", "/audit", "/services"];
    for (let index = 1; index <= 9; index++) {
      await act(async () => {
        dom.window.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: String(index), ctrlKey: true, bubbles: true }));
        await Promise.resolve();
      });
      expect(dom.window.location.pathname).toBe(paths[index - 1]);
    }
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
      await Promise.resolve();
    });
    expect(host.querySelector('[role="dialog"]')).not.toBeNull();
    await click(must(host.querySelector('[aria-label="Fleet settings"]')));
    expect(dom.window.location.pathname).toBe("/settings");
  });

  it("toggles strip stages and clears stage and failure filters together", async () => {
    api.fetchWorkItems.mockResolvedValue([work("failed-plan", "acme/alpha", "failed"), work("queued-plan", "acme/alpha", "queued")]);
    await mount(<App />);
    await click(must(host.querySelector(".f-strip__fail")));
    expect(host.querySelector(".rt-chip--failed.active")).not.toBeNull();
    await click(must(host.querySelector(".f-strip__clear")));
    expect(host.querySelector(".rt-chip--failed.active")).toBeNull();
    const stage = must(host.querySelector(".f-strip__node"));
    await click(stage);
    expect(stage.getAttribute("aria-pressed")).toBe("true");
    await click(stage);
    expect(stage.getAttribute("aria-pressed")).toBe("false");
  });

  it("clears a failed chip when the palette selects Code", async () => {
    api.fetchWorkItems.mockResolvedValue([work("failed-plan", "acme/alpha", "failed"), work("queued-plan", "acme/alpha", "queued")]);
    await mount(<App />);
    await click(must(host.querySelector(".f-strip__fail")));
    expect(host.querySelector(".rt-chip--failed.active")).not.toBeNull();
    await click(must([...host.querySelectorAll("button.f-side__item")].find((button) => button.textContent === "Mission Control")));
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
      await Promise.resolve();
    });
    await click(must([...host.querySelectorAll('[role="option"]')].find((row) => row.textContent.includes("Filter board · Code stage"))));
    expect(dom.window.location.pathname).toBe("/pipeline");
    expect(host.querySelector(".rt-chip--failed.active")).toBeNull();
  });

  it("keeps a live console across views and closes it when project scope changes", async () => {
    await mount(<App />);
    await click(must([...host.querySelectorAll("button")].find((button) => button.textContent === "Live agent console")));
    expect(host.querySelector(".mc-term-modal")?.textContent).toContain("Alpha agent");
    await click(must([...host.querySelectorAll("button.f-side__item")].find((button) => button.textContent === "Pipeline")));
    expect(host.querySelector(".mc-term-modal")?.textContent).toContain("Alpha agent");
    dom.window.history.pushState({}, "", "/p/beta/running");
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.PopStateEvent("popstate"));
      await Promise.resolve();
    });
    expect(host.querySelector(".mc-term-modal")).toBeNull();
  });

  it("keeps a scoped Active Tasks console across views without polling hidden tasks", async () => {
    api.fetchWorkItems.mockResolvedValue([work("alpha-task", "acme/alpha", "running"), work("beta-task", "acme/beta", "running")]);
    api.fetchLiveAgents.mockResolvedValue({ rmux_enabled: true, agents: [{ correlation_key: "alpha-task", ws_path: "/console", title: "Alpha agent", phase: null, streamable: true }] });
    dom.window.history.replaceState({}, "", "/p/alpha/settings");
    await mount(<App />);
    expect(api.fetchLiveAgents).not.toHaveBeenCalled();
    await click(must([...host.querySelectorAll("button.f-side__item")].find((button) => button.textContent === "Active tasks")));
    expect(api.fetchLiveAgents).toHaveBeenCalled();
    await click(must(host.querySelector(".rt-console-btn")));
    expect(host.querySelector(".mc-term-modal")?.textContent).toContain("Alpha agent");
    await click(must([...host.querySelectorAll("button.f-side__item")].find((button) => button.textContent === "Pipeline")));
    expect(host.querySelector(".mc-term-modal")?.textContent).toContain("Alpha agent");
    dom.window.history.pushState({}, "", "/p/beta/running");
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.PopStateEvent("popstate"));
      await Promise.resolve();
    });
    expect(host.querySelector(".mc-term-modal")).toBeNull();
  });

  it("keeps Observe in a new tab and mounts when storage is blocked", async () => {
    vi.spyOn(dom.window.Storage.prototype, "getItem").mockImplementation(() => { throw new dom.window.DOMException("blocked", "SecurityError"); });
    vi.spyOn(dom.window.Storage.prototype, "setItem").mockImplementation(() => { throw new dom.window.DOMException("blocked", "SecurityError"); });
    await mount(<App />);
    expect(host.textContent).toContain("Mission Control");
    const observe = must(host.querySelector<HTMLAnchorElement>('a[href="https://observe.freundcloud.org.uk"]'));
    expect(observe.target).toBe("_blank");
    expect(observe.rel).toContain("noopener");
    await click(must(host.querySelector('[aria-label="Collapse sidebar"]')));
    expect(host.textContent).toContain("Mission Control");
    vi.restoreAllMocks();
  });

  it("caps finished cards at eight and restores project expansion", async () => {
    api.fetchWorkItems.mockResolvedValue(Array.from({ length: 9 }, (_, i) => work(`done-${String(i)}`, "acme/alpha", "done")));
    dom.window.history.replaceState({}, "", "/p/alpha/pipeline");
    await mount(<App />);
    await click(must([...host.querySelectorAll("button")].find((button) => button.textContent.startsWith("Finished"))));
    expect(host.querySelectorAll(".board-finished .f-card")).toHaveLength(8);
    await click(must([...host.querySelectorAll("button")].find((button) => button.textContent === "Show 1 more")));
    expect(host.querySelectorAll(".board-finished .f-card")).toHaveLength(9);
    await click(must(host.querySelector('[aria-label="Expand done-0"]')));
    expect(host.querySelector('[aria-label="Collapse done-0"]')).not.toBeNull();
    await click(must([...host.querySelectorAll("button.f-side__item")].find((button) => button.textContent === "Mission Control")));
    await click(must([...host.querySelectorAll("button.f-side__item")].find((button) => button.textContent === "Pipeline")));
    await click(must([...host.querySelectorAll("button")].find((button) => button.textContent.startsWith("Finished"))));
    expect(host.querySelector('[aria-label="Collapse done-0"]')).not.toBeNull();
  });

  it("shows the worker subtask when Pipeline progress has no percentage", async () => {
    api.fetchWorkItems.mockResolvedValue([work("alpha-task", "acme/alpha", "running")]);
    api.fetchProgress.mockResolvedValue([{ correlation_key: "alpha-task", service: "aifactory", phase: "Coding", percent: null, subtask: "Writing tests", updated_at: "2026-01-01" }]);
    dom.window.history.replaceState({}, "", "/p/alpha/pipeline");
    await mount(<App />);
    expect(host.querySelector(".f-card__subtitle")?.textContent).toContain("Writing tests");
  });

  it("keeps Open available on pending and rejected pipeline cards", async () => {
    api.fetchWorkItems.mockResolvedValue([work("pending-plan", "acme/alpha", "pending"), work("rejected-plan", "acme/alpha", "rejected")]);
    dom.window.history.replaceState({}, "", "/pipeline");
    await mount(<App />);
    for (const key of ["pending-plan", "rejected-plan"]) {
      if (key === "rejected-plan") await click(must([...host.querySelectorAll("button")].find((button) => button.textContent.startsWith("Finished"))));
      const card = must([...host.querySelectorAll(".f-card")].find((item) => item.textContent.includes(key)));
      await click(must(card.querySelector(".f-card__open")));
      expect(host.querySelector('[role="dialog"]')).not.toBeNull();
      const approve = must(host.querySelector<HTMLButtonElement>(".act--approve"));
      expect(approve.getAttribute("aria-disabled")).toBe("true");
      await click(approve);
      expect(api.proposeAction).not.toHaveBeenCalled();
      await click(must(host.querySelector('[aria-label="Close task detail"]')));
    }
  });

  it("restores breadcrumb focus after selection and Escape from footer", async () => {
    const picked = vi.fn();
    const viewAll = vi.fn();
    await mount(
      <ProjectBreadcrumb
        projects={[{ slug: "alpha", name: "Alpha" }]}
        currentSlug="missing"
        onSelect={picked}
        onViewAll={viewAll}
      />,
    );
    expect(host.textContent).toContain("Choose a project");
    const trigger = must(host.querySelector(".proj-bc-btn"));
    await click(trigger);
    await act(async () => {
      must(host.querySelector("input")).dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      await Promise.resolve();
    });
    expect(picked).toHaveBeenCalledWith("alpha");
    expect(dom.window.document.activeElement).toBe(trigger);
    await click(trigger);
    const footer = must(host.querySelector(".proj-bc-all"));
    await act(async () => {
      footer.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
      await Promise.resolve();
    });
    expect(dom.window.document.activeElement).toBe(trigger);
    expect(host.querySelector(".proj-bc-menu")).toBeNull();
    await click(trigger);
    await click(must(host.querySelector(".proj-bc-all")));
    expect(viewAll).toHaveBeenCalledOnce();
    expect(dom.window.document.activeElement).toBe(trigger);
  });

  it("only links resolved linked projects in the switcher", async () => {
    await mount(<PortalSwitcher project={undefined} />);
    expect(host.querySelector('a[href*="/p/"]')).toBeNull();
  });

  it("creates a scoped backlog card in the project repository", async () => {
    await mount(
      <BacklogView
        reloadSignal={0}
        project={{
          id: "beta",
          name: "Beta",
          createdAt: "",
          repositoryIds: [2],
          repoPaths: ["acme/beta"],
        }}
        defaultRepositoryId={1}
      />,
    );
    await click(
      must(
        [...host.querySelectorAll("button")].find((button) =>
          button.textContent.includes("New card"),
        ),
      ),
    );
    const title = must(host.querySelector<HTMLInputElement>('input[placeholder="Title"]'));
    await act(async () => {
      title.focus();
      const setter = must(
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")?.set?.bind(title),
      );
      setter("New beta card");
      title.dispatchEvent(new dom.window.KeyboardEvent("keyup", { key: "d", bubbles: true }));
      title.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    await click(
      must(
        [...host.querySelectorAll("button")].find((button) => button.textContent.includes("Save")),
      ),
    );
    expect(api.createCard).toHaveBeenCalledWith(
      expect.objectContaining({ repository_id: 2, title: "New beta card" }),
    );
  });

  it("shows only the selected project's sync health", async () => {
    await mount(
      <PlanningBoard
        reloadSignal={0}
        project={{
          id: "alpha",
          name: "Alpha",
          createdAt: "",
          repositoryIds: [1],
          repoPaths: ["acme/alpha"],
        }}
        defaultRepositoryId={1}
      />,
    );
    expect(host.querySelector(".card-sync__age")?.textContent).toContain("STALE");
  });

  it("filters federated task hits to scoped item keys", async () => {
    api.searchWorkItems.mockResolvedValue([
      {
        correlation_key: "beta-task",
        title: "Beta secret",
        services: [],
        repo: "acme/beta",
        status: null,
        score: 1,
        matched_on: [],
        updated_at: null,
      },
    ]);
    function Palette() {
      const [open, setOpen] = useState(true);
      return (
        <CommandPalette
          open={open}
          onClose={() => {
            setOpen(false);
          }}
          commands={[]}
          onOpenTask={vi.fn()}
          taskKeys={new Set(["alpha-task"])}
          initialQuery="Beta"
        />
      );
    }
    await mount(<Palette />);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 180));
    });
    expect(host.textContent).not.toContain("Beta secret");
  });

  it("switches to Pipeline with Failed chip selected when clicking the strip failure badge, showing finished/other-stage failures (#576 B15)", async () => {
    api.fetchWorkItems.mockResolvedValueOnce([
      // A task whose plan stage failed and has no active stage (would otherwise be in Finished)
      WorkItemSchema.parse({
        correlation_key: "plan-fail-finished",
        title: "Plan failed task",
        timeline: [],
        pfactory: { task_id: "pf-1", status: "failed", phase: "plan", repo: "acme/alpha" },
        aifactory: { task_id: null, status: null, phase: null },
        tfactory: { task_id: null, status: null, phase: null },
      }),
      // A task whose code stage failed while test stage is still running (in another active column)
      WorkItemSchema.parse({
        correlation_key: "code-fail-active-test",
        title: "Code failed but test active",
        timeline: [],
        pfactory: { task_id: "pf-2", status: "done", phase: "plan", repo: "acme/alpha" },
        aifactory: { task_id: "ai-2", status: "failed", phase: "code", repo: "acme/alpha" },
        tfactory: { task_id: "tf-2", status: "running", phase: "test", repo: "acme/alpha" },
      }),
      work("running-no-failure", "acme/alpha", "running"),
      work("queued-no-failure", "acme/alpha", "queued"),
    ]);
    dom.window.history.replaceState({}, "", "/");
    await mount(<App />);
    const failBadges = [...host.querySelectorAll("button.f-strip__fail")];
    expect(failBadges.length).toBeGreaterThanOrEqual(2);
    // Click the Plan stage failure badge
    await click(must(failBadges[0]));
    expect(dom.window.location.pathname).toBe("/pipeline");
    expect(host.querySelector(".rt-chip--failed.active")).not.toBeNull();
    expect(host.textContent).toContain("Plan failed task");

    // Click the Code stage failure badge — shows the task even though its activeStage is tfactory
    const codeFailBadge = must(
      [...host.querySelectorAll("button.f-strip__fail")].find((b) =>
        (b.getAttribute("aria-label") ?? "").includes("Code"),
      ),
    );
    await click(codeFailBadge);
    expect(host.textContent).toContain("Code failed but test active");
    await click(must([...host.querySelectorAll("button")].find((b) => b.textContent.includes("Finished"))));
    expect(host.querySelector(".board-finished")?.textContent).not.toContain("Code failed but test active");
    expect(host.querySelector(".board-finished")?.textContent).not.toContain("Plan failed task");
    expect(host.querySelector(".board-finished")?.textContent).not.toContain("running-no-failure");
    expect(host.querySelector(".board-finished")?.textContent).not.toContain("queued-no-failure");
    await click(must([...host.querySelectorAll("button")].find((b) => b.textContent.startsWith("All "))));
    expect(host.querySelector(".rt-chip--failed.active")).toBeNull();
    await click(must(host.querySelector(".f-side__item:not([aria-current])")));
    await click(must([...host.querySelectorAll("button.f-side__item")].find((b) => b.textContent === "Pipeline")));
    expect(host.querySelector(".rt-chip--failed.active")).toBeNull();
  });

  it("reopens a compact done card on PlanningBoard and persists local Dismiss / Show hidden in NeedsYouView (#576 B14, B16)", async () => {
    const doneCard = CardSchema.parse({
      ...card("alpha-task", 1),
      status: "done",
      updated_at: "2026-07-26T10:00:00Z",
    });
    api.fetchCards.mockResolvedValue([doneCard]);
    await mount(<PlanningBoard reloadSignal={0} />);
    const doneCol = must(host.querySelector(".column--card-done"));
    expect(doneCol.querySelector(".card-pl--compact")).not.toBeNull();
    expect(doneCol.textContent).not.toContain("▲");
    const status = must(doneCol.querySelector<HTMLSelectElement>("select.card-select--move"));
    await act(async () => {
      status.value = "ready";
      status.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    expect(api.patchCard).toHaveBeenCalledWith("alpha-task", { status: "ready" });
    expect(host.querySelector(".column--card-ready .card-pl:not(.card-pl--compact)")).not.toBeNull();

    // Dismiss in NeedsYouView hides the item into localStorage and offers Show hidden
    const reviewItem = WorkItemSchema.parse({
      correlation_key: "needs-1",
      title: "Review me",
      timeline: [],
      updated_at: new Date().toISOString(),
      pfactory: { task_id: "sess-1", status: "human_review", phase: "plan", repo: "acme/alpha" },
      aifactory: { task_id: null, status: null, phase: null },
      tfactory: { task_id: null, status: null, phase: null },
    });
    api.fetchWorkItems.mockResolvedValue([reviewItem]);
    dom.window.history.replaceState({}, "", "/needs");
    await mount(<App />);
    expect(host.textContent).toContain("Review me");
    const dismissBtn = must(
      [...host.querySelectorAll("button.ny-dismiss")].find((b) => b.textContent === "Dismiss"),
    );
    await click(dismissBtn);
    expect(host.querySelector(".ny")?.textContent).not.toContain("Review me");
    await mount(<div />);
    await mount(<App />);
    expect(host.querySelector(".ny")?.textContent).not.toContain("Review me");
    const showHiddenBtn = must(
      [...host.querySelectorAll("button.ny-chip")].find((b) =>
        b.textContent.includes("Show hidden"),
      ),
    );
    await click(showHiddenBtn);
    expect(host.textContent).toContain("Review me");
    await click(must(host.querySelector("button.ny-open")));
    expect(host.querySelector('[role="dialog"]')).not.toBeNull();
    const codeReview = WorkItemSchema.parse({
      ...reviewItem,
      pfactory: { ...reviewItem.pfactory, status: "done" },
      aifactory: { ...reviewItem.aifactory, status: "human_review" },
    });
    api.fetchWorkItems.mockResolvedValue([codeReview]);
    await mount(<div />);
    await mount(<App />);
    expect(host.textContent).toContain("Review me");
  });
});

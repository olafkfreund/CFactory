import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { JSDOM } from "jsdom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CardSchema, GitConnectionsSchema, WorkItemSchema } from "./api";
import type { Card, GitConnections, WorkItem } from "./api";
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
  fetchSettings: vi.fn(),
  fetchGitConnections: vi.fn(),
  fetchProgress: vi.fn(),
  fetchHealth: vi.fn(),
  fetchCardSyncState: vi.fn(),
  createCard: vi.fn(),
  searchWorkItems: vi.fn(),
  openFeed: vi.fn(),
  refresh: vi.fn(),
}));
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  ...api,
}));
vi.mock("./LiveAgents", () => ({ default: () => null, AgentTerminal: () => null }));

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
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: (callback: FrameRequestCallback) => setTimeout(callback, 0),
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
  api.fetchSettings.mockResolvedValue({ tenant: "default" });
  api.fetchGitConnections.mockResolvedValue(connections);
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
    api.fetchGitConnections.mockRejectedValueOnce(new Error("connection unavailable"));
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
    api.fetchGitConnections.mockResolvedValueOnce(
      GitConnectionsSchema.parse({
        ...connections,
        connections: [
          {
            ...connections.connections[0],
            repositories: [
              {
                id: 1,
                connection_id: 1,
                tenant_id: "default",
                project: "acme/shared",
                aifactory_project_id: "alpha",
              },
              {
                id: 2,
                connection_id: 1,
                tenant_id: "default",
                project: "acme/shared",
                aifactory_project_id: "beta",
              },
            ],
          },
        ],
      }),
    );
    dom.window.history.replaceState({}, "", "/p/acme-shared");
    await mount(<App />);
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
});

describe("mounted project controls", () => {
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
});

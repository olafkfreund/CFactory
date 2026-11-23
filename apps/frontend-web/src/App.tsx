// The cockpit shell: the composition root that wires the sidebar/topbar layout,
// the pipeline strip, the routed views, the event ticker, the Copilot FAB and the
// toast stack. The data/notification plumbing lives in useDashboard, the board in
// Board.tsx, and the shared types/constants/helpers in dashboard.ts (#115).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import AuditView from "./AuditView";
import BacklogView from "./BacklogView";
import PlanningBoard from "./PlanningBoard";
import CommandPalette, { type PaletteCommand } from "./CommandPalette";
import TaskDetail from "./TaskDetail";
import CopilotPanel from "./CopilotPanel";
import MissionControl from "./MissionControl";
import NeedsYouView from "./NeedsYouView";
import { attentionList } from "./needsYou";
import ServicesView from "./ServicesView";
import SettingsView from "./SettingsView";
import RunningTasksView from "./RunningTasksView";
import AgentConsoleModal from "./AgentConsoleModal";
import type { LiveAgent } from "./api";
import TokensView from "./TokensView";
import EventTicker from "./EventTicker";
import { IconExternal, IconRobot, IconRefresh, IconPipeline } from "./icons";
import { NAV, OBSERVE_URL, STAGES } from "./dashboard";
import { activeStage } from "./taskState";
import { stageFailed, type StripStage } from "./pipelineStages";
import { AppShell, type AppShellLabels } from "./components/factory-ui/AppShell";
import type { PipelineStripProps } from "./components/factory-ui/PipelineStrip";
import { applyTheme, setTheme, storedTheme, type Theme } from "./theme";
import { useDashboard } from "./useDashboard";
import { Board, type BoardChip } from "./Board";
import { BackendPill } from "./BackendPill";
import { Toasts } from "./Toasts";

const SHELL_LABELS: AppShellLabels = {
  brand: "CFactory", portals: "Factory portals", portalNames: { plan: "Plan", build: "Build", test: "Test", cockpit: "Cockpit", meet: "Meet" },
  navigation: "Cockpit navigation", openNavigation: "Open navigation", closeNavigation: "Close navigation",
  expandSidebar: "Expand sidebar", collapseSidebar: "Collapse sidebar", projectSettings: "Fleet settings",
  theme: "Colour theme", themeOptions: { light: "Light", dark: "Dark", system: "System" },
  search: "Search", searchShortcut: "Ctrl+K", needsYou: (count) => `${String(count)} need attention`, userMenu: (name) => `${name} menu`,
  project: { project: "Project", selectProject: "Select project", searchProjects: "Search projects", projects: "Projects", recentProjects: "Recent projects", noProjects: "No projects", allProjects: "All projects", addProject: "Add project" },
  pipeline: { pipeline: "PARR pipeline", clearFilter: "Clear filter", failures: (count) => `${String(count)} failed`, filterFailures: (stage) => `Show ${stage.label} failures` },
};

export default function App() {
  const {
    backend,
    items,
    view,
    setView,
    projectRef,
    activeProject,
    activeProjectSlug,
    breadcrumbProjects,
    connectionsLoaded,
    connectionsError,
    defaultRepositoryId,
    setProjectSlug,
    busy,
    live,
    error,
    tick,
    progress,
    toasts,
    copilotOpen,
    setCopilotOpen,
    stageFilter,
    pins,
    alarm,
    tickerOpen,
    dismissToast,
    unpin,
    toggleTicker,
    onSelectStage,
    onRefresh,
    retryProjects,
  } = useDashboard();

  // Command palette (#147): ⌘K / Ctrl-K toggles it anywhere. Task-open is held at
  // the shell so the palette can surface a task in TaskDetail without touching the
  // Board/Running views; the item is re-derived from the live list each render so
  // it stays fresh across refreshes.
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [boardChip, setBoardChip] = useState<BoardChip>("all");
  const [theme, setThemeState] = useState<Theme>(storedTheme);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return window.localStorage.getItem("cfactory-sidebar-collapsed") === "true"; }
    catch { return false; }
  });
  const [consoleAgent, setConsoleAgent] = useState<{ agent: LiveAgent; projectId: string | null } | null>(null);
  const [boardChipSignal, setBoardChipSignal] = useState(0);
  const consumeBoardChip = useCallback(() => {}, []);
  useEffect(() => {
    if (theme !== "system" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const update = () => applyTheme("system");
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [theme]);
  // Deep-link (#149): another portal can jump straight to a task's cross-portal
  // view via /?task=<key>. Seed the open task from the URL on first render; the
  // param is stripped just after (below) so it doesn't linger on refresh/close.
  const [openTaskKey, setOpenTaskKey] = useState<string | null>(() => {
    const t = new URLSearchParams(window.location.search).get("task");
    return t && t.trim() ? t.trim() : null;
  });
  const needsCount = useMemo(() => attentionList(items).length, [items]);
  const stripStages = useMemo(() => STAGES.map((stage) => {
    let count = 0;
    let failed = 0;
    for (const item of items) {
      if (activeStage({ pfactory: item.pfactory.status, aifactory: item.aifactory.status, tfactory: item.tfactory.status }) === stage.key) count++;
      if (stageFailed(item, stage.key)) failed++;
    }
    return { key: stage.key, label: stage.label, service: stage.svc, count, failed, tone: stage.cls === "code" ? "build" as const : stage.cls };
  }), [items]);
  const selectStage = useCallback((key: string | null) => {
    setBoardChip("all");
    setBoardChipSignal((n) => n + 1);
    onSelectStage(key as StripStage | null);
  }, [onSelectStage]);
  const strip: PipelineStripProps = {
    stages: stripStages,
    activeStage: stageFilter,
    onSelectStage: selectStage,
    onSelectFailed: (key) => {
      setBoardChip("failed");
      setBoardChipSignal((n) => n + 1);
      onSelectStage(key as StripStage);
    },
    ...(alarm ? { alarm: { key: alarm.stage, event: alarm.tick } } : {}),
  };
  const scoped = projectRef !== null;
  const consoleScope = activeProject?.id ?? null;
  const openConsole = (agent: LiveAgent) => setConsoleAgent({ agent, projectId: consoleScope });
  const previousConsoleScope = useRef(consoleScope);
  useEffect(() => {
    if (previousConsoleScope.current !== consoleScope) setConsoleAgent(null);
    previousConsoleScope.current = consoleScope;
  }, [consoleScope]);
  const openFleetSettings = () => {
    window.history.pushState({}, "", "/settings");
    window.dispatchEvent(new window.PopStateEvent("popstate"));
  };
  const openTask = openTaskKey
    ? (items.find((w) => w.correlation_key === openTaskKey) ?? null)
    : null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      } else if ((e.metaKey || e.ctrlKey) && /^[1-9]$/.test(e.key)) {
        const entry = NAV[Number(e.key) - 1];
        if (entry) {
          e.preventDefault();
          setView(entry.id);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setView]);

  // Strip the ?task= deep-link param after it has seeded the open task, so a
  // later refresh or close returns to the plain dashboard URL.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).has("task")) {
      const url = new URL(window.location.href);
      url.searchParams.delete("task");
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    }
  }, []);

  const commands: PaletteCommand[] = useMemo(() => {
    const nav: PaletteCommand[] = NAV.map(({ id, label, Icon }) => ({
      id: `nav-${id}`,
      group: "Go to",
      label: scoped && ["tokens", "audit", "services"].includes(id) ? `${label} (fleet-wide)` : label,
      keywords: label,
      Icon,
      run: () => {
        setView(id);
      },
    }));
    const actions: PaletteCommand[] = [
      {
        id: "act-refresh",
        group: "Actions",
        label: "Refresh data",
        keywords: "reload sync",
        Icon: IconRefresh,
        run: () => {
          void onRefresh();
        },
      },
      {
        id: "act-copilot",
        group: "Actions",
        label: "Toggle Copilot",
        keywords: "assistant chat ask",
        Icon: IconRobot,
        run: () => {
          setCopilotOpen((o) => !o);
        },
      },
      {
        id: "act-plan",
        group: "Actions",
        label: "Filter board · Plan stage",
        hint: "1",
        keywords: "pfactory pipeline plan",
        Icon: IconPipeline,
        run: () => {
          selectStage("pfactory");
        },
      },
      {
        id: "act-code",
        group: "Actions",
        label: "Filter board · Code stage",
        hint: "2",
        keywords: "aifactory pipeline build code",
        Icon: IconPipeline,
        run: () => {
          selectStage("aifactory");
        },
      },
      {
        id: "act-test",
        group: "Actions",
        label: "Filter board · Test stage",
        hint: "3",
        keywords: "tfactory pipeline verify test",
        Icon: IconPipeline,
        run: () => {
          selectStage("tfactory");
        },
      },
    ];
    if (OBSERVE_URL) {
      actions.push({
        id: "act-observe",
        group: "Actions",
        label: "Open Observe (metrics)",
        keywords: "openobserve otlp logs metrics",
        Icon: IconExternal,
        run: () => {
          window.open(OBSERVE_URL, "_blank", "noopener,noreferrer");
        },
      });
    }
    return [...nav, ...actions.filter((action) => !scoped || action.id !== "act-copilot")];
  }, [setView, onRefresh, setCopilotOpen, selectStage, scoped]);

  return (
    <AppShell
      portal="cockpit"
      portalProject={activeProject && activeProjectSlug && !activeProject.id.startsWith("repo:") ? { slug: activeProjectSlug, portals: ["plan", "build", "test", "cockpit"] } : undefined}
      nav={NAV.filter((entry) => entry.id !== "settings").map(({ id, label, Icon }) => ({ id, label: scoped && ["tokens", "audit", "services"].includes(id) ? `${label} (fleet-wide)` : label, icon: <Icon size={18} />, ...(id === "needs" ? { badge: needsCount } : {}) }))}
      activeNav={view}
      onNavigate={(id) => setView(id as typeof view)}
      projects={breadcrumbProjects}
      currentProject={activeProjectSlug ?? undefined}
      onSelectProject={setProjectSlug}
      onAllProjects={() => setProjectSlug(null)}
      view={view === "preferences" ? "Preferences" : NAV.find((entry) => entry.id === view)?.label ?? "Cockpit"}
      projectSettings={{ active: view === "settings", onOpen: () => setView("settings") }}
      sidebarFooter={OBSERVE_URL ? <a className="f-side__item" href={OBSERVE_URL} target="_blank" rel="noopener noreferrer"><IconExternal size={18} />Observe</a> : undefined}
      strip={strip}
      topbarStatus={<><span className={`live ${live ? "live--on" : "live--off"}`}>{live ? "● live" : "○ offline"}</span><BackendPill state={backend} /><button className="f-icon-btn" onClick={() => { void onRefresh(); }} disabled={busy} title="Refresh" aria-label="Refresh"><IconRefresh size={16} /></button></>}
      sidebarCollapsed={sidebarCollapsed}
      onSidebarCollapsedChange={(collapsed) => { setSidebarCollapsed(collapsed); try { window.localStorage.setItem("cfactory-sidebar-collapsed", String(collapsed)); } catch { /* preference unavailable */ } }}
      user={{ name: "Account", menu: [{ id: "preferences", label: "Preferences", onSelect: () => setView("preferences") }] }}
      theme={theme}
      onTheme={(next) => { setThemeState(next); setTheme(next); }}
      onSearch={() => setPaletteOpen(true)}
      needsYou={{ count: needsCount, onOpen: () => setView("needs") }}
      labels={scoped ? { ...SHELL_LABELS, projectSettings: "Project settings" } : SHELL_LABELS}
    >
        <div className="workspace">
          {alarm && <div key={alarm.tick} className="alarm-sweep" aria-hidden="true" />}
          <div className="content">
            {error && <div className="banner banner--error">{error}</div>}
            {projectRef !== null && connectionsError ? (
              <div className="mc-panel">
                <h2 className="panel-title">Could not load projects</h2>
                <p>{connectionsError}</p>
                <button type="button" className="primary-btn" onClick={() => { void retryProjects(); }}>Retry</button>
              </div>
            ) : projectRef !== null && !connectionsLoaded ? (
              <p className="col-empty">Loading project…</p>
            ) : projectRef !== null && !activeProject ? (
              <div className="mc-panel">
                <h2 className="panel-title">Choose a project</h2>
                <p>No unique project matches {projectRef}.</p>
                {breadcrumbProjects.map((project) => <button key={project.slug} type="button" className="primary-btn" onClick={() => setProjectSlug(project.slug)}>{project.name}</button>)}
                <button
                  type="button"
                  className="primary-btn"
                  onClick={() => {
                    setProjectSlug(null);
                  }}
                >
                  All projects
                </button>
              </div>
            ) : (
              <>
                {view === "overview" && <MissionControl items={items} reloadSignal={tick} projectScoped={scoped} onOpenConsole={openConsole} />}
                {view === "needs" && (
                  <NeedsYouView
                    items={items}
                    progress={progress}
                    projectScoped={scoped}
                  />
                )}
                {view === "backlog" && (
                  <BacklogView
                    reloadSignal={tick}
                    project={activeProject}
                    defaultRepositoryId={defaultRepositoryId}
                  />
                )}
                {view === "planning" && (
                  <PlanningBoard
                    reloadSignal={tick}
                    project={activeProject}
                    defaultRepositoryId={defaultRepositoryId}
                  />
                )}
                {view === "pipeline" && (
                  <Board
                    items={items}
                    progress={progress}
                    stageFilter={stageFilter}
                    onClearStage={() => selectStage(null)}
                    initialChip={boardChip}
                    chipSignal={boardChipSignal}
                    onChipConsumed={consumeBoardChip}
                    onChipChange={setBoardChip}
                    projectId={activeProject?.id}
                  />
                )}
                {view === "tokens" && <TokensView reloadSignal={tick} />}
                {view === "audit" && <AuditView reloadSignal={tick} />}
                {view === "services" && <ServicesView backend={backend} reloadSignal={tick} />}
                {view === "settings" && (scoped && activeProject ? <section className="mc-panel"><h1>Project Settings</h1><p>{activeProject.name}</p><p>Repositories: {activeProject.repoPaths.join(", ")}</p><button type="button" className="primary-btn" onClick={openFleetSettings}>Fleet settings</button></section> : <SettingsView reloadSignal={tick} />)}
                {view === "preferences" && <section className="mc-panel"><h1>Preferences</h1><p>Colour theme</p><div role="group" aria-label="Colour theme">{(["light", "dark", "system"] as const).map((option) => <button key={option} type="button" className={`set-toggle-btn ${theme === option ? "active" : ""}`} aria-pressed={theme === option} onClick={() => { setThemeState(option); setTheme(option); }}>{option}</button>)}</div><button type="button" className="primary-btn" onClick={openFleetSettings}>Fleet settings</button></section>}
              </>
            )}
            <div hidden={view !== "running"}><RunningTasksView items={items} progress={progress} visible={view === "running"} onOpenConsole={openConsole} /></div>
          </div>
          <EventTicker
            items={items}
            pins={pins}
            onUnpin={unpin}
            open={tickerOpen}
            onToggle={toggleTicker}
          />
        </div>
      {/* Copilot — floating assistant (robot FAB → chat popup) */}
      {copilotOpen && !scoped && (
        <div className="copilot-pop" role="dialog" aria-label="Copilot assistant">
          <div className="copilot-pop__head">
            <span className="copilot-pop__title">
              <IconRobot size={15} /> Copilot
            </span>
            <button
              className="copilot-pop__x"
              onClick={() => setCopilotOpen(false)}
              aria-label="Close Copilot"
            >
              ×
            </button>
          </div>
          <div className="copilot-pop__body">
            <CopilotPanel reloadSignal={tick} compact />
          </div>
        </div>
      )}
      {!scoped && <button
        className={`copilot-fab ${copilotOpen ? "copilot-fab--open" : ""}`}
        onClick={() => setCopilotOpen((o) => !o)}
        title={copilotOpen ? "Close Copilot" : "Ask Copilot"}
        aria-label="Toggle Copilot"
        aria-expanded={copilotOpen}
      >
        <IconRobot size={24} />
      </button>}

      <Toasts toasts={toasts} onDismiss={dismissToast} />
      {consoleAgent && consoleAgent.projectId === consoleScope && (!scoped || items.some((item) => item.correlation_key === consoleAgent.agent.correlation_key)) && <AgentConsoleModal agent={consoleAgent.agent} onClose={() => setConsoleAgent(null)} />}

      {/* Command palette (⌘K) + the task it opens, held at the shell (#147) */}
      <CommandPalette
        open={paletteOpen}
        onClose={() => {
          setPaletteOpen(false);
        }}
        commands={commands}
        projects={breadcrumbProjects}
        onSwitchProject={setProjectSlug}
        onOpenTask={(correlationKey) => {
          if (items.some((item) => item.correlation_key === correlationKey)) setOpenTaskKey(correlationKey);
        }}
        taskKeys={scoped ? new Set(items.map((item) => item.correlation_key)) : undefined}
      />
      {openTask && (
        <TaskDetail
          wi={openTask}
          lp={progress[openTask.correlation_key]}
          onClose={() => {
            setOpenTaskKey(null);
          }}
          onActed={() => {
            void onRefresh();
          }}
        />
      )}
    </AppShell>
  );
}

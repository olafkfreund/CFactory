// The cockpit shell: the composition root that wires the sidebar/topbar layout,
// the pipeline strip, the routed views, the event ticker, the Copilot FAB and the
// toast stack. The data/notification plumbing lives in useDashboard, the board in
// Board.tsx, and the shared types/constants/helpers in dashboard.ts (#115).
import { useEffect, useMemo, useState } from "react";
import AuditView from "./AuditView";
import CommandPalette, { type PaletteCommand } from "./CommandPalette";
import TaskDetail from "./TaskDetail";
import ThemeToggle from "./ThemeToggle";
import PortalSwitcher from "./PortalSwitcher";
import CopilotPanel from "./CopilotPanel";
import MissionControl from "./MissionControl";
import NeedsYouView from "./NeedsYouView";
import { attentionList } from "./needsYou";
import ServicesView from "./ServicesView";
import SettingsView from "./SettingsView";
import RunningTasksView from "./RunningTasksView";
import TokensView from "./TokensView";
import PipelineStrip from "./PipelineStrip";
import EventTicker from "./EventTicker";
import { IconBrand, IconExternal, IconRobot, IconLogout, IconRefresh, IconPipeline } from "./icons";
import { NAV, OBSERVE_URL } from "./dashboard";
import { useDashboard } from "./useDashboard";
import { Board } from "./Board";
import { BackendPill } from "./BackendPill";
import { Toasts } from "./Toasts";

export default function App() {
  const {
    backend,
    items,
    view,
    setView,
    busy,
    live,
    error,
    tick,
    progress,
    toasts,
    copilotOpen,
    setCopilotOpen,
    stageFilter,
    setStageFilter,
    pins,
    alarm,
    tickerOpen,
    dismissToast,
    unpin,
    toggleTicker,
    onSelectStage,
    onRefresh,
  } = useDashboard();

  // Command palette (#147): ⌘K / Ctrl-K toggles it anywhere. Task-open is held at
  // the shell so the palette can surface a task in TaskDetail without touching the
  // Board/Running views; the item is re-derived from the live list each render so
  // it stays fresh across refreshes.
  const [paletteOpen, setPaletteOpen] = useState(false);
  // Deep-link (#149): another portal can jump straight to a task's cross-portal
  // view via /?task=<key>. Seed the open task from the URL on first render; the
  // param is stripped just after (below) so it doesn't linger on refresh/close.
  const [openTaskKey, setOpenTaskKey] = useState<string | null>(() => {
    const t = new URLSearchParams(window.location.search).get("task");
    return t && t.trim() ? t.trim() : null;
  });
  const needsCount = useMemo(() => attentionList(items).length, [items]);
  const openTask = openTaskKey
    ? (items.find((w) => w.correlation_key === openTaskKey) ?? null)
    : null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

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
      label,
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
          onSelectStage("pfactory");
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
          onSelectStage("aifactory");
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
          onSelectStage("tfactory");
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
    return [...nav, ...actions];
  }, [setView, onRefresh, setCopilotOpen, onSelectStage]);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <IconBrand size={20} />
          </span>
          <span className="brand-name">CFactory</span>
        </div>
        <div className="side-label">
          COCKPIT
          <span className="side-sub">control tower · PARR pipeline</span>
        </div>
        <nav className="nav">
          {NAV.map(({ id, label, Icon }) => (
            <button
              key={id}
              className={`nav-item ${view === id ? "active" : ""}`}
              onClick={() => setView(id)}
            >
              <Icon size={18} />
              <span>{label}</span>
              {id === "needs" && needsCount > 0 && (
                <span className="nav-badge" aria-label={`${needsCount} need attention`}>
                  {needsCount}
                </span>
              )}
            </button>
          ))}
          {OBSERVE_URL && (
            <a
              className="nav-item nav-item--ext"
              href={OBSERVE_URL}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the OpenObserve / OTLP backend in a new tab"
            >
              <IconExternal size={18} />
              <span>Observe</span>
            </a>
          )}
        </nav>
        <div className="side-foot">
          <button className="primary-btn" onClick={onRefresh} disabled={busy}>
            <IconRefresh size={16} />
            {busy ? "Refreshing…" : "Refresh"}
          </button>
          <button className="side-logout">
            <IconLogout size={15} /> Log Out
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <PortalSwitcher needsCount={needsCount} />
          <div className="tabs">
            <span className="tab active">{NAV.find((n) => n.id === view)?.label}</span>
          </div>
          <div className="topbar-right">
            <ThemeToggle />
            <button
              className="cmdk-trigger"
              onClick={() => {
                setPaletteOpen(true);
              }}
              title="Search or jump to anything"
              aria-label="Open command palette"
            >
              <span className="cmdk-trigger-q" aria-hidden="true" />
              <span>Search…</span>
              <kbd>⌘K</kbd>
            </button>
            <span className={`live ${live ? "live--on" : "live--off"}`}>
              {live ? "● live" : "○ offline"}
            </span>
            <BackendPill state={backend} />
            <button className="icon-btn" onClick={onRefresh} disabled={busy} title="Refresh">
              <IconRefresh size={16} />
            </button>
          </div>
        </header>

        <PipelineStrip items={items} active={stageFilter} onSelect={onSelectStage} alarm={alarm} />

        <div className="workspace">
          {alarm && <div key={alarm.tick} className="alarm-sweep" aria-hidden="true" />}
          <main className="content">
            {error && <div className="banner banner--error">{error}</div>}
            {view === "overview" && <MissionControl items={items} reloadSignal={tick} />}
            {view === "needs" && <NeedsYouView items={items} progress={progress} />}
            {view === "pipeline" && (
              <Board
                items={items}
                progress={progress}
                stageFilter={stageFilter}
                onClearStage={() => setStageFilter(null)}
              />
            )}
            {view === "running" && <RunningTasksView items={items} progress={progress} />}
            {view === "tokens" && <TokensView reloadSignal={tick} />}
            {view === "audit" && <AuditView reloadSignal={tick} />}
            {view === "services" && <ServicesView backend={backend} reloadSignal={tick} />}
            {view === "settings" && <SettingsView reloadSignal={tick} />}
          </main>
          <EventTicker
            items={items}
            pins={pins}
            onUnpin={unpin}
            open={tickerOpen}
            onToggle={toggleTicker}
          />
        </div>
      </div>

      {/* Copilot — floating assistant (robot FAB → chat popup) */}
      {copilotOpen && (
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
      <button
        className={`copilot-fab ${copilotOpen ? "copilot-fab--open" : ""}`}
        onClick={() => setCopilotOpen((o) => !o)}
        title={copilotOpen ? "Close Copilot" : "Ask Copilot"}
        aria-label="Toggle Copilot"
        aria-expanded={copilotOpen}
      >
        <IconRobot size={24} />
      </button>

      <Toasts toasts={toasts} onDismiss={dismissToast} />

      {/* Command palette (⌘K) + the task it opens, held at the shell (#147) */}
      <CommandPalette
        open={paletteOpen}
        onClose={() => {
          setPaletteOpen(false);
        }}
        commands={commands}
        onOpenTask={(correlationKey) => {
          setOpenTaskKey(correlationKey);
        }}
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
    </div>
  );
}

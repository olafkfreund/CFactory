// The cockpit shell: the composition root that wires the sidebar/topbar layout,
// the pipeline strip, the routed views, the event ticker, the Copilot FAB and the
// toast stack. The data/notification plumbing lives in useDashboard, the board in
// Board.tsx, and the shared types/constants/helpers in dashboard.ts (#115).
import AuditView from "./AuditView";
import CopilotPanel from "./CopilotPanel";
import MissionControl from "./MissionControl";
import ServicesView from "./ServicesView";
import SettingsView from "./SettingsView";
import RunningTasksView from "./RunningTasksView";
import TokensView from "./TokensView";
import PipelineStrip from "./PipelineStrip";
import EventTicker from "./EventTicker";
import { IconBrand, IconExternal, IconRobot, IconLogout, IconRefresh } from "./icons";
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
          <div className="tabs">
            <span className="tab active">{NAV.find((n) => n.id === view)?.label}</span>
          </div>
          <div className="topbar-right">
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
    </div>
  );
}

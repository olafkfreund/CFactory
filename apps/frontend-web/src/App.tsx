import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import AuditView from "./AuditView";
import CopilotPanel from "./CopilotPanel";
import MissionControl from "./MissionControl";
import ServicesView from "./ServicesView";
import SettingsView from "./SettingsView";
import RunningTasksView from "./RunningTasksView";
import TokensView from "./TokensView";
import TaskDetail from "./TaskDetail";
import PipelineStrip, { type StripAlarm, type StripStage } from "./PipelineStrip";
import EventTicker, { type TickerPin } from "./EventTicker";
import { AnimatePresence, motion } from "framer-motion";
import {
  fetchHealth,
  fetchProgress,
  fetchWorkItems,
  openFeed,
  refresh as apiRefresh,
  type Health,
  type LiveProgress,
  type ServiceState,
  type WorkItem,
} from "./api";
import { diffEvents } from "./taskEvents";
import { ensureNotifyPermission, osNotify, EVENT_LABEL, type EventKind, type TaskEvent } from "./notify";
import { activeStage, overallState, stageState, STATE_LABEL, type TaskState } from "./taskState";
import { displayTitle, keySlug } from "./correlationKey";
import {
  IconAudit,
  IconBrand,
  IconClock,
  IconRobot,
  IconInsights,
  IconLogout,
  IconPipeline,
  IconRefresh,
  IconRunning,
  IconServices,
  IconSettings,
  IconTokens,
} from "./icons";

type View =
  | "overview"
  | "pipeline"
  | "running"
  | "tokens"
  | "audit"
  | "services"
  | "settings";
interface ToastEntry {
  id: number;
  ev: TaskEvent;
}
type Backend =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

const NAV: { id: View; label: string; Icon: typeof IconPipeline }[] = [
  { id: "overview", label: "Mission Control", Icon: IconInsights },
  { id: "pipeline", label: "Pipeline", Icon: IconPipeline },
  { id: "running", label: "Active tasks", Icon: IconRunning },
  { id: "tokens", label: "Tokens", Icon: IconTokens },
  { id: "audit", label: "Audit", Icon: IconAudit },
  { id: "services", label: "Services", Icon: IconServices },
  { id: "settings", label: "Settings", Icon: IconSettings },
];

const STAGES = [
  { key: "pfactory", label: "Plan", svc: "PFactory", cls: "plan" },
  { key: "aifactory", label: "Code", svc: "AIFactory", cls: "code" },
  { key: "tfactory", label: "Test", svc: "TFactory", cls: "test" },
] as const;

type StageKey = (typeof STAGES)[number]["key"];

function relTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 90) return "just now";
  const m = s / 60;
  if (m < 90) return `${Math.round(m)}m ago`;
  const h = m / 60;
  if (h < 36) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/** The canonical overall lifecycle state of one work item. */
function itemState(wi: WorkItem): TaskState {
  return overallState([
    stageState(wi.pfactory.status),
    stageState(wi.aifactory.status),
    stageState(wi.tfactory.status),
  ]);
}

const TICKER_PREF = "cfactory-ticker-open";

export default function App() {
  const [backend, setBackend] = useState<Backend>({ kind: "loading" });
  const [items, setItems] = useState<WorkItem[]>([]);
  const [view, setView] = useState<View>("overview");
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [progress, setProgress] = useState<Record<string, LiveProgress>>({});
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const [copilotOpen, setCopilotOpen] = useState(false);
  const [stageFilter, setStageFilter] = useState<StripStage | null>(null);
  const [pins, setPins] = useState<TickerPin[]>([]);
  const [alarm, setAlarm] = useState<StripAlarm | null>(null);
  const [tickerOpen, setTickerOpen] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(TICKER_PREF) !== "0";
    } catch {
      return true;
    }
  });

  // Notification plumbing: remember each task's last overall state so we only
  // alert on genuine transitions, and seed the baseline silently on first load.
  const statesRef = useRef<Map<string, TaskState>>(new Map());
  const seededRef = useRef(false);
  const toastIdRef = useRef(0);
  const pinIdRef = useRef(0);
  const alarmTickRef = useRef(0);

  const ingest = useCallback((incoming: WorkItem[]) => {
    const events = diffEvents(statesRef.current, incoming, seededRef.current);
    if (!seededRef.current) {
      seededRef.current = true;
      return; // first hydrate is not "news"
    }
    if (events.length === 0) return;
    for (const ev of events) osNotify(ev);

    // Failures take over the frame: pin them in the ticker, flare the affected
    // pipeline-strip node, and run the one-shot border sweep (CSS, PRM-gated).
    const failures = events.filter((ev) => ev.kind === "failed");
    if (failures.length > 0) {
      setPins((prev) =>
        [
          ...failures.map((ev) => ({ id: ++pinIdRef.current, key: ev.key, title: ev.title, at: Date.now() })),
          ...prev,
        ].slice(0, 4),
      );
      const latest = failures[failures.length - 1];
      const wi = incoming.find((w) => w.correlation_key === latest.key);
      const stage = wi
        ? (["tfactory", "aifactory", "pfactory"] as const).find((k) => stageState(wi[k].status) === "failed")
        : undefined;
      setAlarm({ stage: stage ?? "aifactory", tick: ++alarmTickRef.current });
    }

    setToasts((prev) => {
      const next = [...prev];
      for (const ev of events) next.push({ id: ++toastIdRef.current, ev });
      // Failure toasts persist until dismissed; keep the rest of the stack short.
      const failed = next.filter((t) => t.ev.kind === "failed").slice(-4);
      const others = next.filter((t) => t.ev.kind !== "failed").slice(-4);
      return [...failed, ...others].sort((a, b) => a.id - b.id);
    });
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const unpin = useCallback((id: number) => {
    setPins((prev) => prev.filter((p) => p.id !== id));
  }, []);

  const toggleTicker = useCallback(() => {
    setTickerOpen((open) => {
      try {
        window.localStorage.setItem(TICKER_PREF, open ? "0" : "1");
      } catch {
        /* private mode */
      }
      return !open;
    });
  }, []);

  const onSelectStage = useCallback((stage: StripStage | null) => {
    setStageFilter(stage);
    if (stage) setView("pipeline"); // the board is where the stage filter lives
  }, []);

  const load = useCallback(async () => {
    try {
      const fresh = await fetchWorkItems();
      setItems(fresh);
      ingest(fresh);
      setError(null);
      setTick((t) => t + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    fetchProgress()
      .then((ps) => setProgress(Object.fromEntries(ps.map((p) => [p.correlation_key, p]))))
      .catch(() => undefined);
  }, [ingest]);

  useEffect(() => {
    ensureNotifyPermission();
    fetchHealth()
      .then((health) => setBackend({ kind: "ok", health }))
      .catch((e: unknown) =>
        setBackend({ kind: "error", message: e instanceof Error ? e.message : String(e) }),
      );
    void load();
  }, [load]);

  useEffect(() => {
    const ws = openFeed(
      (msg) => {
        if (msg.type === "snapshot") {
          setItems(msg.items);
          ingest(msg.items);
        } else if (msg.type === "progress")
          setProgress((prev) => ({ ...prev, [msg.item.correlation_key]: msg.item }));
        else {
          setItems((prev) => [
            msg.item,
            ...prev.filter((w) => w.correlation_key !== msg.item.correlation_key),
          ]);
          ingest([msg.item]);
        }
      },
      () => setLive(true),
      () => setLive(false),
    );
    return () => ws.close();
  }, [ingest]);

  const onRefresh = useCallback(async () => {
    setBusy(true);
    try {
      await apiRefresh();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [load]);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark"><IconBrand size={20} /></span>
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
        </nav>
        <div className="side-foot">
          <button className="primary-btn" onClick={onRefresh} disabled={busy}>
            <IconRefresh size={16} />
            {busy ? "Refreshing…" : "Refresh"}
          </button>
          <button className="side-logout"><IconLogout size={15} /> Log Out</button>
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
          <EventTicker items={items} pins={pins} onUnpin={unpin} open={tickerOpen} onToggle={toggleTicker} />
        </div>
      </div>

      {/* Copilot — floating assistant (robot FAB → chat popup) */}
      {copilotOpen && (
        <div className="copilot-pop" role="dialog" aria-label="Copilot assistant">
          <div className="copilot-pop__head">
            <span className="copilot-pop__title"><IconRobot size={15} /> Copilot</span>
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

const TOAST_KIND_CLASS: Record<EventKind, string> = {
  new: "toast--new",
  done: "toast--done",
  failed: "toast--failed",
  review: "toast--review",
};

function Toasts({ toasts, onDismiss }: { toasts: ToastEntry[]; onDismiss: (id: number) => void }) {
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <Toast key={t.id} entry={t} onDismiss={onDismiss} />
        ))}
      </AnimatePresence>
    </div>
  );
}

function Toast({ entry, onDismiss }: { entry: ToastEntry; onDismiss: (id: number) => void }) {
  // Failure toasts persist until the user dismisses them; the rest auto-clear.
  const persistent = entry.ev.kind === "failed";
  useEffect(() => {
    if (persistent) return;
    const id = window.setTimeout(() => onDismiss(entry.id), 7000);
    return () => window.clearTimeout(id);
  }, [entry.id, onDismiss, persistent]);
  return (
    <motion.div
      className={`toast ${TOAST_KIND_CLASS[entry.ev.kind]}`}
      initial={{ opacity: 0, x: 24 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 24, transition: { duration: 0.2 } }}
      onClick={() => onDismiss(entry.id)}
      role="button"
      tabIndex={0}
    >
      <span className="toast-dot" />
      <div className="toast-body">
        <div className="toast-title">
          {EVENT_LABEL[entry.ev.kind]}{" "}
          <span className="toast-key" title={entry.ev.key}>#{keySlug(entry.ev.key)}</span>
        </div>
        <div className="toast-detail">{entry.ev.title}</div>
      </div>
      {persistent && (
        <button
          className="toast-x"
          aria-label="Dismiss failure notification"
          onClick={(e) => {
            e.stopPropagation();
            onDismiss(entry.id);
          }}
        >
          ✕
        </button>
      )}
    </motion.div>
  );
}

type BoardChip = "all" | "running" | "review" | "queued" | "failed";

const BOARD_CHIPS: { id: BoardChip; label: string }[] = [
  { id: "all", label: "All" },
  { id: "running", label: "Running" },
  { id: "review", label: "In review" },
  { id: "queued", label: "Queued" },
  { id: "failed", label: "Failed" },
];

function Board({
  items,
  progress,
  stageFilter,
  onClearStage,
}: {
  items: WorkItem[];
  progress: Record<string, LiveProgress>;
  stageFilter: StripStage | null;
  onClearStage: () => void;
}) {
  const [query, setQuery] = useState("");
  const [chip, setChip] = useState<BoardChip>("all");
  const [showFinished, setShowFinished] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  // Same lifecycle semantics as the Mission Control ring and the pipeline
  // strip: an item lives at its furthest ACTIVE stage; finished/idle items
  // drop into the (hidden by default) finished section instead of inflating
  // the columns.
  const { byStage, finished } = useMemo(() => {
    const m: Record<StageKey, WorkItem[]> = { pfactory: [], aifactory: [], tfactory: [] };
    const finished: WorkItem[] = [];
    for (const wi of items) {
      const stage = activeStage({
        pfactory: wi.pfactory.status,
        aifactory: wi.aifactory.status,
        tfactory: wi.tfactory.status,
      });
      if (stage) m[stage].push(wi);
      else finished.push(wi);
    }
    return { byStage: m, finished };
  }, [items]);

  const q = query.trim().toLowerCase();
  const matchesQuery = useCallback(
    (wi: WorkItem) =>
      !q ||
      wi.correlation_key.toLowerCase().includes(q) ||
      (wi.title ?? "").toLowerCase().includes(q) ||
      displayTitle(wi.title, wi.correlation_key).toLowerCase().includes(q),
    [q],
  );

  const visible = useCallback(
    (wi: WorkItem) => matchesQuery(wi) && (chip === "all" || itemState(wi) === chip),
    [matchesQuery, chip],
  );

  const chipCounts = useMemo(() => {
    const active = [...byStage.pfactory, ...byStage.aifactory, ...byStage.tfactory].filter(matchesQuery);
    const c: Record<BoardChip, number> = { all: active.length, running: 0, review: 0, queued: 0, failed: 0 };
    for (const wi of active) {
      const st = itemState(wi);
      if (st === "running" || st === "review" || st === "queued" || st === "failed") c[st]++;
    }
    return c;
  }, [byStage, matchesQuery]);

  const stages = STAGES.filter((s) => !stageFilter || s.key === stageFilter);
  const shownFinished = finished.filter(matchesQuery);

  // Resolve against the latest items so the open drawer updates live.
  const selected = selectedKey ? items.find((w) => w.correlation_key === selectedKey) ?? null : null;

  return (
    <>
      <div className="page-head">
        <h1>Pipeline</h1>
        <p>Every work item at its current active stage — plan → code → test by GitHub issue. Click a card for live detail.</p>
      </div>

      <div className="rt-toolbar board-toolbar">
        <input
          className="board-search"
          type="search"
          placeholder="Filter by title or key…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Filter work items"
        />
        {BOARD_CHIPS.map((c) => (
          <button
            key={c.id}
            className={`rt-chip ${chip === c.id ? "active" : ""} rt-chip--${c.id}`}
            onClick={() => setChip(c.id)}
          >
            {c.label} <span className="rt-chip-n">{chipCounts[c.id]}</span>
          </button>
        ))}
        <span className="board-toolbar-spacer" />
        {stageFilter && (
          <button className="rt-chip active" onClick={onClearStage} title="Showing one pipeline stage — click to clear">
            stage: {STAGES.find((s) => s.key === stageFilter)?.label} ✕
          </button>
        )}
        <button
          className={`rt-chip ${showFinished ? "active" : ""}`}
          onClick={() => setShowFinished((v) => !v)}
          aria-pressed={showFinished}
        >
          Finished <span className="rt-chip-n">{shownFinished.length}</span>
        </button>
      </div>

      <section
        className="board"
        aria-label="Pipeline board"
        style={{ "--board-cols": stages.length } as CSSProperties}
      >
        {stages.map((stage) => {
          const cards = byStage[stage.key].filter(visible);
          return (
            <div className={`column column--${stage.cls}`} key={stage.key}>
              <div className="column-head">
                <span className="column-dot" />
                <span className="column-title">{stage.label}</span>
                <span className="column-svc">{stage.svc}</span>
                <span className="column-count">{cards.length}</span>
              </div>
              <div className="column-body">
                {cards.length === 0 ? (
                  <p className="col-empty">{byStage[stage.key].length === 0 ? "No active work items" : "Nothing matches the filter"}</p>
                ) : (
                  cards.map((wi) => (
                    <Card
                      key={wi.correlation_key}
                      wi={wi}
                      lp={progress[wi.correlation_key]}
                      onOpen={() => setSelectedKey(wi.correlation_key)}
                    />
                  ))
                )}
              </div>
            </div>
          );
        })}
      </section>

      {showFinished && (
        <section className="board-finished" aria-label="Finished work items">
          <h2>Finished · {shownFinished.length}</h2>
          {shownFinished.length === 0 ? (
            <p className="col-empty">No finished work items{q ? " match the filter" : ""}.</p>
          ) : (
            <div className="board-finished-grid">
              {shownFinished.map((wi) => (
                <Card
                  key={wi.correlation_key}
                  wi={wi}
                  lp={progress[wi.correlation_key]}
                  onOpen={() => setSelectedKey(wi.correlation_key)}
                />
              ))}
            </div>
          )}
        </section>
      )}

      <AnimatePresence>
        {selected && (
          <TaskDetail
            wi={selected}
            lp={progress[selected.correlation_key]}
            onClose={() => setSelectedKey(null)}
            onActed={() => void apiRefresh()}
          />
        )}
      </AnimatePresence>
    </>
  );
}

function Card({ wi, lp, onOpen }: { wi: WorkItem; lp?: LiveProgress; onOpen: () => void }) {
  const last = wi.timeline.length ? wi.timeline[wi.timeline.length - 1].updated_at : null;
  const state = itemState(wi);
  return (
    <article
      className={`card-wi card-wi--clickable ${lp ? "card-wi--live" : ""}`}
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onOpen()}
    >
      <div className="card-wi__head">
        <span className="wi-key" title={wi.correlation_key}>#{keySlug(wi.correlation_key)}</span>
        {lp ? <LiveBadge lp={lp} /> : last && (
          <span className="wi-time"><IconClock /> {relTime(last)}</span>
        )}
      </div>
      <div className="card-wi__title">{displayTitle(wi.title, wi.correlation_key)}</div>
      <div className="stage-chips">
        {STAGES.map((s) => (
          <StageChip key={s.key} label={s.label} cls={s.cls} state={wi[s.key]} />
        ))}
      </div>
      <div className="card-wi__foot">
        {wi.timeline.length} events · {STATE_LABEL[state]}
      </div>
    </article>
  );
}

function LiveBadge({ lp }: { lp: LiveProgress }) {
  return (
    <motion.span
      className="live-badge"
      title={lp.subtask || lp.phase || "active"}
      animate={{ opacity: [1, 0.55, 1] }}
      transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
    >
      <span className="live-badge-dot" />
      {lp.percent != null ? `${Math.round(lp.percent)}%` : lp.phase || "live"}
    </motion.span>
  );
}

function StageChip({ label, cls, state }: { label: string; cls: string; state: ServiceState }) {
  const active = Boolean(state && state.status);
  return (
    <span className={`schip ${active ? `schip--${cls}` : "schip--idle"}`} title={state?.task_id ?? ""}>
      <span className="schip-k">{label}</span>
      <span className="schip-v">{active ? state.status : "—"}</span>
    </span>
  );
}

function BackendPill({ state }: { state: Backend }) {
  if (state.kind === "loading") return <span className="pill pill--wait">connecting…</span>;
  if (state.kind === "error")
    return <span className="pill pill--down" title={state.message}>backend offline</span>;
  const n = Object.keys(state.health.upstreams).length;
  return (
    <span className="pill pill--up" title={JSON.stringify(state.health.upstreams, null, 2)}>
      v{state.health.version} · {n} upstreams
    </span>
  );
}

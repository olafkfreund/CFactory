import { useCallback, useEffect, useMemo, useState } from "react";
import AuditView from "./AuditView";
import CopilotPanel from "./CopilotPanel";
import MissionControl from "./MissionControl";
import ServicesView from "./ServicesView";
import {
  fetchHealth,
  fetchWorkItems,
  openFeed,
  refresh as apiRefresh,
  type Health,
  type ServiceState,
  type WorkItem,
} from "./api";
import {
  IconAudit,
  IconBrand,
  IconClock,
  IconCopilot,
  IconInsights,
  IconLogout,
  IconPipeline,
  IconRefresh,
  IconServices,
} from "./icons";

type View = "overview" | "pipeline" | "copilot" | "audit" | "services";
type Backend =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

const NAV: { id: View; label: string; Icon: typeof IconPipeline }[] = [
  { id: "overview", label: "Mission Control", Icon: IconInsights },
  { id: "pipeline", label: "Pipeline", Icon: IconPipeline },
  { id: "copilot", label: "Copilot", Icon: IconCopilot },
  { id: "audit", label: "Audit", Icon: IconAudit },
  { id: "services", label: "Services", Icon: IconServices },
];

const STAGES = [
  { key: "pfactory", label: "Plan", svc: "PFactory", cls: "plan" },
  { key: "aifactory", label: "Code", svc: "AIFactory", cls: "code" },
  { key: "tfactory", label: "Test", svc: "TFactory", cls: "test" },
] as const;

type StageKey = (typeof STAGES)[number]["key"];

function furthestStage(wi: WorkItem): StageKey {
  if (wi.tfactory.status) return "tfactory";
  if (wi.aifactory.status) return "aifactory";
  return "pfactory";
}

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

export default function App() {
  const [backend, setBackend] = useState<Backend>({ kind: "loading" });
  const [items, setItems] = useState<WorkItem[]>([]);
  const [view, setView] = useState<View>("overview");
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const load = useCallback(async () => {
    try {
      setItems(await fetchWorkItems());
      setError(null);
      setTick((t) => t + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
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
        if (msg.type === "snapshot") setItems(msg.items);
        else
          setItems((prev) => [
            msg.item,
            ...prev.filter((w) => w.correlation_key !== msg.item.correlation_key),
          ]);
      },
      () => setLive(true),
      () => setLive(false),
    );
    return () => ws.close();
  }, []);

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

        <main className="content">
          {error && <div className="banner banner--error">{error}</div>}
          {view === "overview" && <MissionControl items={items} reloadSignal={tick} />}
          {view === "pipeline" && <Board items={items} />}
          {view === "copilot" && <CopilotPanel reloadSignal={tick} />}
          {view === "audit" && <AuditView reloadSignal={tick} />}
          {view === "services" && <ServicesView backend={backend} />}
        </main>
      </div>
    </div>
  );
}

function Board({ items }: { items: WorkItem[] }) {
  const byStage = useMemo(() => {
    const m: Record<StageKey, WorkItem[]> = { pfactory: [], aifactory: [], tfactory: [] };
    for (const wi of items) m[furthestStage(wi)].push(wi);
    return m;
  }, [items]);

  return (
    <>
      <div className="page-head">
        <h1>Pipeline</h1>
        <p>Every work item threaded across plan → code → test by GitHub issue.</p>
      </div>
      <section className="board" aria-label="Pipeline board">
        {STAGES.map((stage) => (
          <div className={`column column--${stage.cls}`} key={stage.key}>
            <div className="column-head">
              <span className="column-dot" />
              <span className="column-title">{stage.label}</span>
              <span className="column-svc">{stage.svc}</span>
              <span className="column-count">{byStage[stage.key].length}</span>
            </div>
            <div className="column-body">
              {byStage[stage.key].length === 0 ? (
                <p className="col-empty">No work items</p>
              ) : (
                byStage[stage.key].map((wi) => <Card key={wi.correlation_key} wi={wi} />)
              )}
            </div>
          </div>
        ))}
      </section>
    </>
  );
}

function Card({ wi }: { wi: WorkItem }) {
  const last = wi.timeline.length ? wi.timeline[wi.timeline.length - 1].updated_at : null;
  return (
    <article className="card-wi">
      <div className="card-wi__head">
        <span className="wi-key">#{wi.correlation_key}</span>
        {last && (
          <span className="wi-time"><IconClock /> {relTime(last)}</span>
        )}
      </div>
      <div className="card-wi__title">{wi.title || "Untitled work item"}</div>
      <div className="stage-chips">
        {STAGES.map((s) => (
          <StageChip key={s.key} label={s.label} cls={s.cls} state={wi[s.key]} />
        ))}
      </div>
      <div className="card-wi__foot">{wi.timeline.length} events</div>
    </article>
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

import { useCallback, useEffect, useState } from "react";
import {
  fetchHealth,
  fetchWorkItems,
  refresh,
  type Health,
  type ServiceState,
  type WorkItem,
} from "./api";

type BackendState =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

const STAGES = [
  { key: "pfactory", label: "Plan", service: "PFactory" },
  { key: "aifactory", label: "Code", service: "AIFactory" },
  { key: "tfactory", label: "Test", service: "TFactory" },
] as const;

export default function App() {
  const [backend, setBackend] = useState<BackendState>({ kind: "loading" });
  const [items, setItems] = useState<WorkItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await fetchWorkItems());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    fetchHealth()
      .then((health) => setBackend({ kind: "ok", health }))
      .catch((err: unknown) =>
        setBackend({ kind: "error", message: err instanceof Error ? err.message : String(err) }),
      );
    void load();
  }, [load]);

  const onRefresh = useCallback(async () => {
    setBusy(true);
    try {
      await refresh();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [load]);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" aria-hidden="true" />
          CFactory
          <span className="brand-sub">cockpit</span>
        </div>
        <div className="topbar-right">
          <BackendPill state={backend} />
          <button className="btn" onClick={onRefresh} disabled={busy}>
            {busy ? "refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      <main className="main">
        <section className="hero">
          <h1>Pipeline control tower</h1>
          <p>
            Every work item, threaded across PFactory → AIFactory → TFactory by its
            GitHub issue. Hit <strong>Refresh</strong> to poll the services.
          </p>
        </section>

        {error && <div className="banner banner--error">{error}</div>}

        <section className="board" aria-label="Pipeline board">
          <div className="board-head">
            <div className="cell cell--key">Work item</div>
            {STAGES.map((s) => (
              <div className="cell" key={s.key}>
                {s.label} <span className="cell-svc">{s.service}</span>
              </div>
            ))}
          </div>

          {items.length === 0 ? (
            <p className="empty">No work items yet — Refresh to poll the services.</p>
          ) : (
            items.map((wi) => (
              <div className="board-row" key={wi.correlation_key}>
                <div className="cell cell--key">
                  <span className="wi-key">#{wi.correlation_key}</span>
                  {wi.title && <span className="wi-title">{wi.title}</span>}
                </div>
                {STAGES.map((s) => (
                  <div className="cell" key={s.key}>
                    <StatusBadge state={wi[s.key]} />
                  </div>
                ))}
              </div>
            ))
          )}
        </section>
      </main>
    </div>
  );
}

function StatusBadge({ state }: { state: ServiceState }) {
  if (!state || !state.status) {
    return <span className="badge badge--idle">—</span>;
  }
  return <span className="badge badge--active" title={state.task_id ?? ""}>{state.status}</span>;
}

function BackendPill({ state }: { state: BackendState }) {
  if (state.kind === "loading") {
    return <span className="pill pill--wait">connecting…</span>;
  }
  if (state.kind === "error") {
    return <span className="pill pill--down" title={state.message}>backend offline</span>;
  }
  const count = Object.keys(state.health.upstreams).length;
  return (
    <span className="pill pill--up" title={JSON.stringify(state.health.upstreams, null, 2)}>
      backend v{state.health.version} · {count} upstreams
    </span>
  );
}

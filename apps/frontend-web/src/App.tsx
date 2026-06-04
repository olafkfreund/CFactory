import { useEffect, useState } from "react";
import { fetchHealth, type Health } from "./api";

type BackendState =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

const STAGES = [
  { key: "plan", label: "Plan", service: "PFactory" },
  { key: "code", label: "Code", service: "AIFactory" },
  { key: "test", label: "Test", service: "TFactory" },
] as const;

export default function App() {
  const [state, setState] = useState<BackendState>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    fetchHealth()
      .then((health) => active && setState({ kind: "ok", health }))
      .catch((err: unknown) =>
        active &&
        setState({ kind: "error", message: err instanceof Error ? err.message : String(err) }),
      );
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" aria-hidden="true" />
          CFactory
          <span className="brand-sub">cockpit</span>
        </div>
        <BackendPill state={state} />
      </header>

      <main className="main">
        <section className="hero">
          <h1>Pipeline control tower</h1>
          <p>
            One pane of glass across PFactory → AIFactory → TFactory. The board
            below will thread each WorkItem through plan, code and test.
          </p>
        </section>

        <section className="board" aria-label="Pipeline board">
          {STAGES.map((stage) => (
            <div className="column" key={stage.key}>
              <div className="column-head">
                <span className="column-title">{stage.label}</span>
                <span className="column-svc">{stage.service}</span>
              </div>
              <div className="column-body">
                <p className="empty">No work items yet</p>
              </div>
            </div>
          ))}
        </section>
      </main>
    </div>
  );
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

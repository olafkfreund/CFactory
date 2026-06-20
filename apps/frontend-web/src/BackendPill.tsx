// The topbar backend-health pill (connecting / offline / up + upstream count),
// extracted from App.tsx (#115). Pure extraction — behaviour-preserving.
import type { Backend } from "./dashboard";

export function BackendPill({ state }: { state: Backend }) {
  if (state.kind === "loading") return <span className="pill pill--wait">connecting…</span>;
  if (state.kind === "error")
    return (
      <span className="pill pill--down" title={state.message}>
        backend offline
      </span>
    );
  const n = Object.keys(state.health.upstreams).length;
  return (
    <span className="pill pill--up" title={JSON.stringify(state.health.upstreams, null, 2)}>
      v{state.health.version} · {n} upstreams
    </span>
  );
}

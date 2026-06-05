import type { Health } from "./api";

type Backend =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

const ROLE: Record<string, { role: string; cls: string }> = {
  pfactory: { role: "Plan", cls: "plan" },
  aifactory: { role: "Code", cls: "code" },
  tfactory: { role: "Test", cls: "test" },
};

export default function ServicesView({ backend }: { backend: Backend }) {
  const health = backend.kind === "ok" ? backend.health : null;
  return (
    <>
      <div className="page-head">
        <h1>Services</h1>
        <p>The cockpit and the three upstream services it threads together.</p>
      </div>

      <div className="svc-grid">
        <div className="svc-card svc-card--self">
          <div className="svc-top">
            <span className="svc-name">CFactory</span>
            <span className={`status-pill ${health ? "ok" : "fail"}`}>
              <span className="dot" /> {health ? "online" : backend.kind === "loading" ? "…" : "offline"}
            </span>
          </div>
          <div className="svc-role">Cockpit · control tower</div>
          <dl className="svc-meta">
            <div><dt>version</dt><dd className="mono">{health?.version ?? "—"}</dd></div>
            <div><dt>multi-tenant</dt><dd className="mono">{health ? String(health.multi_tenant ?? false) : "—"}</dd></div>
          </dl>
        </div>

        {health &&
          Object.entries(health.upstreams).map(([name, url]) => {
            const meta = ROLE[name] ?? { role: "—", cls: "plan" };
            return (
              <div className={`svc-card svc-card--${meta.cls}`} key={name}>
                <div className="svc-top">
                  <span className="svc-name">{name}</span>
                  <span className="svc-role-pill">{meta.role}</span>
                </div>
                <div className="svc-role">endpoint configured</div>
                <div className="svc-url mono">{url}</div>
              </div>
            );
          })}
      </div>

      {!health && backend.kind === "error" && (
        <div className="banner banner--error">backend offline — {backend.message}</div>
      )}
    </>
  );
}

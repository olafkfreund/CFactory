import { useEffect, useState, type ComponentType, type SVGProps } from "react";
import { fetchServices, type Health, type ServiceStatus } from "./api";
import { IconControlTower, IconDocument, IconRobot, IconFlask } from "./icons";

type Backend =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

type IconCmp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

const ROLE: Record<string, { role: string; cls: string; Icon: IconCmp }> = {
  pfactory: { role: "Plan", cls: "plan", Icon: IconDocument },
  aifactory: { role: "Code", cls: "code", Icon: IconRobot },
  tfactory: { role: "Test", cls: "test", Icon: IconFlask },
};

export default function ServicesView({ backend, reloadSignal }: { backend: Backend; reloadSignal: number }) {
  const health = backend.kind === "ok" ? backend.health : null;
  // null = still probing; {} = probe failed / none reachable
  const [statuses, setStatuses] = useState<Record<string, ServiceStatus> | null>(null);

  useEffect(() => {
    let alive = true;
    setStatuses(null);
    fetchServices()
      .then((list) => {
        if (alive) setStatuses(Object.fromEntries(list.map((s) => [s.name, s])));
      })
      .catch(() => {
        if (alive) setStatuses({});
      });
    return () => {
      alive = false;
    };
  }, [reloadSignal]);

  return (
    <>
      <div className="page-head">
        <h1>Services</h1>
        <p>The cockpit and the three upstream services it threads together.</p>
      </div>

      <div className="svc-grid">
        <div className="svc-card svc-card--self">
          <div className="svc-top">
            <span className="svc-ident">
              <span className="svc-ico"><IconControlTower size={17} /></span>
              <span className="svc-name">CFactory</span>
            </span>
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
            const meta = ROLE[name] ?? { role: "—", cls: "plan", Icon: IconDocument };
            const Icon = meta.Icon;
            const pending = statuses === null;
            const online = statuses?.[name]?.online ?? false;
            return (
              <div className={`svc-card svc-card--${meta.cls}`} key={name}>
                <div className="svc-top">
                  <span className="svc-ident">
                    <span className="svc-ico"><Icon size={17} /></span>
                    <span className="svc-name">{name}</span>
                  </span>
                  <span className={`status-pill ${pending ? "" : online ? "ok" : "fail"}`}>
                    <span className="dot" /> {pending ? "…" : online ? "online" : "offline"}
                  </span>
                </div>
                <div className="svc-role"><span className="svc-role-pill">{meta.role}</span> endpoint configured</div>
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

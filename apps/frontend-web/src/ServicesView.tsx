import { useEffect, useState, type ComponentType, type SVGProps } from "react";
import { fetchProviderHealth, fetchServices, updateService, type Health, type ProviderAuthEntry, type ServiceStatus } from "./api";
import {
  IconControlTower,
  IconDocument,
  IconRobot,
  IconFlask,
  IconEdit,
  IconInsights,
  IconExternal,
} from "./icons";

type Backend =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

type IconCmp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

const ROLE: Record<string, { role: string; cls: string; Icon: IconCmp; label?: string }> = {
  pfactory: { role: "Plan", cls: "plan", Icon: IconDocument },
  aifactory: { role: "Code", cls: "code", Icon: IconRobot },
  tfactory: { role: "Test", cls: "test", Icon: IconFlask },
  // OpenObserve telemetry backend — reachability-only (not a PARR factory, not an
  // editable upstream). Its endpoint is set in the deployment manifest, not the UI.
  observe: { role: "Observe", cls: "observe", Icon: IconInsights, label: "Observe (OpenObserve)" },
};

// Upstreams whose endpoint can be edited from this view (the PARR factories).
// `observe` is configured via CFACTORY_OBSERVE_API_URL in the deployment manifest,
// so it has no inline editor and no link to the runtime PUT endpoint.
const EDITABLE = new Set(["pfactory", "aifactory", "tfactory"]);

// Link out to the OpenObserve UI (the same VITE_OBSERVE_URL used by App.tsx, #87).
// Build-time configurable; empty string hides the link. The observe Services row
// links here so the operator can jump straight to the telemetry dashboard.
const OBSERVE_URL =
  import.meta.env.VITE_OBSERVE_URL === undefined
    ? "https://observe.freundcloud.org.uk"
    : import.meta.env.VITE_OBSERVE_URL;

// Map the upstream probe status to a pill style + label. A reachable-but-
// rejecting upstream (401/403) is amber "auth error", not a green "online".
const PILL: Record<string, { cls: string; label: string }> = {
  online: { cls: "ok", label: "online" },
  unauthorized: { cls: "warn", label: "auth error" },
  offline: { cls: "fail", label: "offline" },
  error: { cls: "fail", label: "error" },
};

function pillFor(s: ServiceStatus | undefined): { cls: string; label: string; detail?: string | null } {
  if (!s) return { cls: "fail", label: "offline" };
  // Fall back to the legacy `online` boolean if an older backend omits `status`.
  const key = s.status ?? (s.online ? "online" : "offline");
  return { ...(PILL[key] ?? PILL.error), detail: s.detail };
}

export default function ServicesView({ backend, reloadSignal }: { backend: Backend; reloadSignal: number }) {
  const health = backend.kind === "ok" ? backend.health : null;
  const [statuses, setStatuses] = useState<Record<string, ServiceStatus> | null>(null);
  // Provider-auth health (#109): per-service credential config pre-flight.
  const [providerAuth, setProviderAuth] = useState<Record<string, ProviderAuthEntry>>({});

  // Inline endpoint editing.
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [urlOverride, setUrlOverride] = useState<Record<string, string>>({});

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
    fetchProviderHealth()
      .then((ph) => {
        if (alive) setProviderAuth(Object.fromEntries(ph.services.map((s) => [s.name, s])));
      })
      .catch(() => {
        if (alive) setProviderAuth({});
      });
    return () => {
      alive = false;
    };
  }, [reloadSignal]);

  function startEdit(name: string, url: string) {
    setEditing(name);
    setDraft(url);
    setSaveErr(null);
  }
  function cancelEdit() {
    setEditing(null);
    setSaveErr(null);
  }
  function save(name: string) {
    setSaving(true);
    setSaveErr(null);
    updateService(name, draft.trim())
      .then((res) => {
        setUrlOverride((o) => ({ ...o, [name]: res.url }));
        setStatuses((s) => ({ ...(s ?? {}), [name]: res }));
        setEditing(null);
      })
      .catch((e: unknown) => setSaveErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setSaving(false));
  }

  return (
    <>
      <div className="page-head">
        <h1>Services</h1>
        <p>The cockpit and the three upstream services it threads together — endpoints are editable.</p>
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
            const pill = pillFor(statuses?.[name]);
            const shownUrl = urlOverride[name] ?? url;
            const editable = EDITABLE.has(name);
            const isEditing = editing === name && editable;
            // The observe row links out to the OpenObserve UI (when configured).
            const linkUrl = name === "observe" ? OBSERVE_URL : "";
            return (
              <div className={`svc-card svc-card--${meta.cls}`} key={name}>
                <div className="svc-top">
                  <span className="svc-ident">
                    <span className="svc-ico"><Icon size={17} /></span>
                    <span className="svc-name">{meta.label ?? name}</span>
                  </span>
                  <span
                    className={`status-pill ${pending ? "" : pill.cls}`}
                    title={pending ? undefined : pill.detail ?? undefined}
                  >
                    <span className="dot" /> {pending ? "…" : pill.label}
                  </span>
                </div>
                <div className="svc-role"><span className="svc-role-pill">{meta.role}</span> endpoint configured</div>

                {(() => {
                  // Provider-auth pre-flight tile (#109): alert when the service
                  // reports it has NO usable provider credential.
                  const pa = providerAuth[name];
                  if (!pa || !pa.reachable) return null;
                  if (!pa.reported) {
                    return <div className="svc-auth svc-auth--unknown">provider auth: unknown</div>;
                  }
                  const configured = pa.providers.filter((p) => p.configured).map((p) => p.name);
                  if (!pa.any_configured) {
                    return (
                      <div className="svc-auth svc-auth--alert" title="No provider credential configured — this service cannot authenticate to any model">
                        provider auth: no credentials configured
                      </div>
                    );
                  }
                  return (
                    <div className="svc-auth svc-auth--ok" title={`Configured providers: ${configured.join(", ")}`}>
                      provider auth: {configured.join(", ")}
                    </div>
                  );
                })()}

                {isEditing ? (
                  <div className="svc-edit">
                    <input
                      className="svc-edit-input mono"
                      value={draft}
                      autoFocus
                      placeholder="http://host:port"
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") save(name);
                        if (e.key === "Escape") cancelEdit();
                      }}
                    />
                    <div className="svc-edit-actions">
                      <button className="svc-btn svc-btn--save" onClick={() => save(name)} disabled={saving}>
                        {saving ? "Saving…" : "Save"}
                      </button>
                      <button className="svc-btn" onClick={cancelEdit} disabled={saving}>Cancel</button>
                    </div>
                    {saveErr && <div className="svc-edit-err">{saveErr}</div>}
                  </div>
                ) : (
                  <div className="svc-url-row">
                    <span className="svc-url mono">{shownUrl}</span>
                    {editable ? (
                      <button className="svc-edit-btn" title="Edit endpoint" aria-label={`Edit ${name} endpoint`} onClick={() => startEdit(name, shownUrl)}>
                        <IconEdit size={14} />
                      </button>
                    ) : linkUrl ? (
                      <a
                        className="svc-edit-btn"
                        href={linkUrl}
                        target="_blank"
                        rel="noreferrer"
                        title="Open OpenObserve"
                        aria-label="Open OpenObserve UI"
                      >
                        <IconExternal size={14} />
                      </a>
                    ) : null}
                  </div>
                )}
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

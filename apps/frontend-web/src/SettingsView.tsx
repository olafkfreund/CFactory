import { useEffect, useState } from "react";
import { fetchSettings, updateCopilotSettings, type CopilotSettings } from "./api";
import GitConnectionsPanel from "./GitConnectionsPanel";
import { IconRobot, IconCheck } from "./icons";

const PROVIDER_LABEL: Record<string, string> = {
  claude: "Claude (Anthropic)",
  ollama: "Ollama Cloud",
};

// A sensible default model when the user switches provider.
function defaultModelFor(provider: string, current: CopilotSettings | null): string {
  if (current && current.provider === provider) return current.model;
  if (provider === "ollama") return current?.models?.[0] ?? "gpt-oss:120b";
  return "claude-opus-4-8";
}

export default function SettingsView({ reloadSignal }: { reloadSignal: number }) {
  const [settings, setSettings] = useState<CopilotSettings | null>(null);
  // Which tenant this cockpit is: the backend resolves it from the header
  // oauth2-proxy injects, so the git-config panel has to be told (RFC-0020 §3.3).
  const [tenant, setTenant] = useState<string | null>(null);
  const [provider, setProvider] = useState("claude");
  const [model, setModel] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchSettings()
      .then((s) => {
        if (!alive) return;
        setSettings(s);
        setTenant(s.tenant);
        setProvider(s.provider);
        setModel(s.model);
        setErr(null);
      })
      .catch((e: unknown) => alive && setErr(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [reloadSignal]);

  function pickProvider(p: string) {
    setProvider(p);
    setModel(defaultModelFor(p, settings));
    setSaved(false);
  }

  function save() {
    setSaving(true);
    setErr(null);
    setSaved(false);
    updateCopilotSettings(provider, model.trim())
      .then((s) => {
        setSettings(s);
        setProvider(s.provider);
        setModel(s.model);
        setSaved(true);
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setSaving(false));
  }

  const dirty = settings ? provider !== settings.provider || model.trim() !== settings.model : false;
  const providers = settings?.providers ?? ["claude", "ollama"];
  const ollama = provider === "ollama";
  const reachable = settings?.reachable;
  const hasKey = settings?.has_key;

  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
        <p>
          Cockpit configuration. Choose which LLM powers the copilot, and which git hosts and
          repositories this board syncs with.
        </p>
      </div>

      {err && <div className="banner banner--error">{err}</div>}

      <div className="set-grid">
        <div className="svc-card svc-card--code set-card">
          <div className="svc-top">
            <span className="svc-ident">
              <span className="svc-ico"><IconRobot size={17} /></span>
              <span className="svc-name">Copilot model</span>
            </span>
            {settings && (
              <span className="status-pill ok">
                <span className="dot" /> active: {settings.provider}
              </span>
            )}
          </div>
          <div className="svc-role">Which provider answers copilot questions</div>

          <div className="set-field">
            <label className="set-label">Provider</label>
            <div className="set-toggle">
              {providers.map((p) => (
                <button
                  key={p}
                  className={`set-toggle-btn ${provider === p ? "active" : ""}`}
                  onClick={() => pickProvider(p)}
                  type="button"
                >
                  {PROVIDER_LABEL[p] ?? p}
                </button>
              ))}
            </div>
          </div>

          <div className="set-field">
            <label className="set-label" htmlFor="set-model">Model</label>
            <input
              id="set-model"
              className="svc-edit-input mono"
              value={model}
              list={ollama ? "ollama-models" : undefined}
              placeholder={ollama ? "gpt-oss:120b" : "claude-opus-4-8"}
              onChange={(e) => {
                setModel(e.target.value);
                setSaved(false);
              }}
            />
            {ollama && settings?.models && (
              <datalist id="ollama-models">
                {settings.models.map((m) => (
                  <option value={m} key={m} />
                ))}
              </datalist>
            )}
          </div>

          {ollama && (
            <div className="set-conn">
              <span className={`status-pill ${reachable ? "ok" : "warn"}`}>
                <span className="dot" />
                {reachable ? "Ollama Cloud reachable" : "Ollama Cloud unreachable"}
              </span>
              {!hasKey && (
                <span className="set-hint">No OLLAMA_API_KEY configured — set it in the deployment.</span>
              )}
              {settings?.base_url && <span className="set-hint mono">{settings.base_url}</span>}
            </div>
          )}

          <div className="set-actions">
            <button className="btn" onClick={save} disabled={saving || !dirty || !model.trim()}>
              {saving ? "Saving…" : "Save"}
            </button>
            {saved && !dirty && (
              <span className="set-saved"><IconCheck size={14} /> Saved</span>
            )}
          </div>
        </div>
      </div>

      <GitConnectionsPanel tenant={tenant} reloadSignal={reloadSignal} />
    </>
  );
}

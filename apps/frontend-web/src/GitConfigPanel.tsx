import { useEffect, useState } from "react";
import {
  deleteGitCredential,
  fetchGitConfig,
  setGitCredential,
  updateGitConfig,
  verifyGitConfig,
  type GitConfig,
} from "./api";
import { IconGitBranch, IconCheck } from "./icons";

// The three hosts the board can be pointed at. Bitbucket and Gitea exist in the
// provider protocol but are unimplemented, so offering them would be a lie.
const PROVIDERS = ["github", "gitlab", "azure_devops"] as const;
const PROVIDER_LABEL: Record<string, string> = {
  github: "GitHub",
  gitlab: "GitLab",
  azure_devops: "Azure DevOps",
};

// What each host calls the thing that goes in the project box. Getting this
// wrong is the most likely mistake in the form, so the placeholder says it.
const PROJECT_PLACEHOLDER: Record<string, string> = {
  github: "owner/repo",
  gitlab: "group/subgroup/project",
  azure_devops: "organization/project/repo",
};

// The derived status: a short state word for the pill, and the sentence that
// explains it as a note under the card's role line. A whole sentence in a pill
// wraps to three lines and runs across the card title (#211), and the
// explanation reads better as body text anyway.
// Deliberately explicit about credential_missing: that state is not a mistake
// the user made, it is the deployment's credential, and saying so avoids an
// afternoon spent re-typing a project path that was always correct.
// Typed as possibly-absent on purpose: a status the backend adds later must fall
// back to showing the raw value rather than rendering "undefined".
const STATUS: Record<string, { tone: string; text: string; note?: string } | undefined> = {
  unconfigured: { tone: "warn", text: "not configured", note: "No project set." },
  credential_missing: {
    tone: "warn",
    text: "no credential",
    note: "No usable credential — the project cannot be reached.",
  },
  configured: { tone: "ok", text: "configured", note: "Not verified yet." },
  verified: { tone: "ok", text: "verified" },
};

function labelsToText(labels: string[]): string {
  return labels.join(", ");
}

function textToLabels(text: string): string[] {
  return text
    .split(",")
    .map((l) => l.trim())
    .filter(Boolean);
}

/**
 * Settings > Git integration (RFC-0020 section 3.3).
 *
 * Which git host this tenant's board syncs with, which project, and which
 * AIFactory project its builds land in — the three things that used to be
 * environment variables nobody could see from the portal.
 */
export default function GitConfigPanel({
  tenant,
  reloadSignal,
}: {
  tenant: string | null;
  reloadSignal: number;
}) {
  const [config, setConfig] = useState<GitConfig | null>(null);
  const [provider, setProvider] = useState("github");
  const [baseUrl, setBaseUrl] = useState("");
  const [project, setProject] = useState("");
  const [intakeProject, setIntakeProject] = useState("");
  const [aifactoryProject, setAifactoryProject] = useState("");
  const [labels, setLabels] = useState("");
  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [verifyNote, setVerifyNote] = useState<string | null>(null);
  // The credential box. Write-only in both directions: it is never populated
  // from a response (there is nothing to populate it WITH), and it is emptied
  // the moment the value has been sent, so it does not sit in the DOM.
  const [credential, setCredential] = useState("");
  const [savingCredential, setSavingCredential] = useState(false);
  const [credentialNote, setCredentialNote] = useState<string | null>(null);

  function load(c: GitConfig) {
    setConfig(c);
    setProvider(c.provider);
    setBaseUrl(c.base_url);
    setProject(c.project ?? "");
    setIntakeProject(c.intake_project ?? "");
    setAifactoryProject(c.aifactory_project_id ?? "");
    setLabels(labelsToText(c.default_labels));
  }

  useEffect(() => {
    if (!tenant) return;
    let alive = true;
    fetchGitConfig(tenant)
      .then((c) => {
        if (!alive) return;
        load(c);
        setErr(null);
      })
      .catch((e: unknown) => {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [tenant, reloadSignal]);

  function save() {
    if (!tenant) return;
    setSaving(true);
    setErr(null);
    setSaved(false);
    setVerifyNote(null);
    updateGitConfig(tenant, {
      provider,
      base_url: baseUrl.trim() || null,
      project: project.trim() || null,
      intake_project: intakeProject.trim() || null,
      aifactory_project_id: aifactoryProject.trim() || null,
      default_labels: textToLabels(labels),
    })
      .then((c) => {
        load(c);
        setSaved(true);
      })
      .catch((e: unknown) => {
        setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        setSaving(false);
      });
  }

  // Re-read the configuration after a credential write: the masked indicator and
  // the derived status both live on it, and both just changed.
  function reload() {
    if (!tenant) return;
    fetchGitConfig(tenant)
      .then(load)
      .catch((e: unknown) => {
        setErr(e instanceof Error ? e.message : String(e));
      });
  }

  function storeCredential() {
    if (!tenant || !credential.trim()) return;
    setSavingCredential(true);
    setErr(null);
    setCredentialNote(null);
    setVerifyNote(null);
    setGitCredential(tenant, credential.trim())
      .then(() => {
        setCredential("");
        setCredentialNote("Credential stored. It cannot be displayed again.");
        reload();
      })
      .catch((e: unknown) => {
        setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        setSavingCredential(false);
      });
  }

  function removeCredential() {
    if (!tenant) return;
    setSavingCredential(true);
    setErr(null);
    setCredentialNote(null);
    deleteGitCredential(tenant)
      .then((r) => {
        setCredentialNote(r.removed ? "Credential removed." : "There was no credential to remove.");
        reload();
      })
      .catch((e: unknown) => {
        setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        setSavingCredential(false);
      });
  }

  function verify() {
    if (!tenant) return;
    setVerifying(true);
    setErr(null);
    setVerifyNote(null);
    verifyGitConfig(tenant)
      .then((r) => {
        if (r.config) load(r.config);
        setVerifyNote(r.ok ? `reached ${r.repository ?? "the project"}` : (r.reason ?? "failed"));
      })
      .catch((e: unknown) => {
        setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        setVerifying(false);
      });
  }

  const status = STATUS[config?.status ?? "unconfigured"] ?? {
    tone: "warn",
    text: config ? config.status : "unknown",
  };
  const dirty =
    config !== null &&
    (provider !== config.provider ||
      baseUrl.trim() !== config.base_url ||
      project.trim() !== (config.project ?? "") ||
      intakeProject.trim() !== (config.intake_project ?? "") ||
      aifactoryProject.trim() !== (config.aifactory_project_id ?? "") ||
      labelsToText(textToLabels(labels)) !== labelsToText(config.default_labels));

  return (
    <div className="svc-card svc-card--code set-card">
      <div className="svc-top">
        <span className="svc-ident">
          <span className="svc-ico"><IconGitBranch size={17} /></span>
          <span className="svc-name">Git integration</span>
        </span>
        <span className={`status-pill ${status.tone}`}>
          <span className="dot" /> {status.text}
        </span>
      </div>
      <div className="svc-role">
        Which host and project this board syncs cards with, and where its builds land
      </div>
      {status.note && <div className="set-status-note">{status.note}</div>}

      {err && <div className="banner banner--error">{err}</div>}

      <div className="set-field">
        <label className="set-label">Provider</label>
        <div className="set-toggle">
          {PROVIDERS.map((p) => (
            <button
              key={p}
              className={`set-toggle-btn ${provider === p ? "active" : ""}`}
              onClick={() => {
                setProvider(p);
                setSaved(false);
              }}
              type="button"
            >
              {PROVIDER_LABEL[p]}
            </button>
          ))}
        </div>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="git-base-url">Host</label>
        <input
          id="git-base-url"
          className="svc-edit-input mono"
          value={baseUrl}
          placeholder="leave empty for the provider default"
          onChange={(e) => {
            setBaseUrl(e.target.value);
            setSaved(false);
          }}
        />
        <span className="set-hint">
          Set this for a self-hosted GitLab, GitHub Enterprise or Azure DevOps Server.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="git-project">Project</label>
        <input
          id="git-project"
          className="svc-edit-input mono"
          value={project}
          placeholder={PROJECT_PLACEHOLDER[provider]}
          onChange={(e) => {
            setProject(e.target.value);
            setSaved(false);
          }}
        />
        <span className="set-hint">
          Where a ready card opens its issue. Empty means cards can only adopt an existing
          issue, never create one.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="git-intake-project">Import from (optional)</label>
        <input
          id="git-intake-project"
          className="svc-edit-input mono"
          value={intakeProject}
          placeholder={`defaults to ${project.trim() || "the project above"}`}
          onChange={(e) => {
            setIntakeProject(e.target.value);
            setSaved(false);
          }}
        />
        <span className="set-hint">
          Import existing issues from a different repository than the one above.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="git-aifactory-project">AIFactory project id</label>
        <input
          id="git-aifactory-project"
          className="svc-edit-input mono"
          value={aifactoryProject}
          placeholder="e.g. 5d78d4b9-35f9-4445-92c1-78f3ff60a494"
          onChange={(e) => {
            setAifactoryProject(e.target.value);
            setSaved(false);
          }}
        />
        <span className="set-hint">
          The AIFactory project a dispatched card is BUILT in — a project id, not a
          repository path. Empty means low/medium cards cannot be dispatched and the code
          and test stages refuse. Builds write real code there, so choose it deliberately.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="git-labels">Default labels (optional)</label>
        <input
          id="git-labels"
          className="svc-edit-input mono"
          value={labels}
          placeholder="comma separated"
          onChange={(e) => {
            setLabels(e.target.value);
            setSaved(false);
          }}
        />
        <span className="set-hint">
          Put on issues the board opens. A factory:&lt;tier&gt; label is refused — it is the
          fleet's intake trigger and would build the same card twice.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="git-credential">Credential</label>
        <div className="set-cred-state">
          {config?.credential.configured ? (
            <span className="set-hint">
              {config.credential.source === "tenant"
                ? `Stored for this tenant${
                    config.credential.updated_at
                      ? ` on ${new Date(config.credential.updated_at).toLocaleDateString()}`
                      : ""
                  }${config.credential.key_version ? ` (key ${config.credential.key_version})` : ""}.`
                : "Using the deployment's environment credential, shared by every tenant."}
            </span>
          ) : (
            <span className="set-hint">No credential stored for this board.</span>
          )}
        </div>
        <input
          id="git-credential"
          className="svc-edit-input mono"
          type="password"
          autoComplete="off"
          value={credential}
          placeholder={config?.credential.configured ? "replace the stored credential" : "paste a provider token"}
          onChange={(e) => {
            setCredential(e.target.value);
            setCredentialNote(null);
          }}
        />
        <span className="set-hint">
          Encrypted at rest and never shown again — not here, not in any API response.
          Replacing it is the only way to change it; Remove revokes it from this board.
          Every use is written to the audit trail.
        </span>
        <div className="set-actions">
          <button
            className="btn"
            onClick={storeCredential}
            disabled={savingCredential || !credential.trim() || !tenant}
            type="button"
          >
            {savingCredential ? "Saving…" : "Store credential"}
          </button>
          <button
            className="btn"
            onClick={removeCredential}
            disabled={savingCredential || config?.credential.source !== "tenant"}
            title={
              config?.credential.source === "tenant"
                ? undefined
                : "Nothing stored for this tenant — the deployment's own credential is set in its environment"
            }
            type="button"
          >
            Remove
          </button>
          {credentialNote && <span className="set-hint">{credentialNote}</span>}
        </div>
      </div>

      <div className="set-conn">
        {config?.source === "env" && (
          <span className="set-hint">
            Still reading the deployment's environment variables. Saving makes this tenant's
            own configuration authoritative.
          </span>
        )}
        {verifyNote && <span className="set-hint">{verifyNote}</span>}
        {config?.verify_error && !verifyNote && (
          <span className="set-hint">last check failed: {config.verify_error}</span>
        )}
      </div>

      <div className="set-actions">
        <button className="btn" onClick={save} disabled={saving || !dirty || !tenant}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          className="btn"
          onClick={verify}
          disabled={verifying || dirty || !config?.project}
          title={dirty ? "Save first — verify checks the stored configuration" : undefined}
        >
          {verifying ? "Checking…" : "Verify"}
        </button>
        {saved && !dirty && (
          <span className="set-saved"><IconCheck size={14} /> Saved</span>
        )}
      </div>
    </div>
  );
}

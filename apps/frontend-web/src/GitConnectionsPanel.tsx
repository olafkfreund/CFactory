import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  createGitConnection,
  createGitRepository,
  deleteConnectionCredential,
  deleteGitConnection,
  deleteGitInstall,
  deleteGitRepository,
  fetchGitCapabilities,
  fetchGitConnections,
  setConnectionCredential,
  setDefaultGitRepository,
  startGitInstall,
  updateGitConnection,
  updateGitRepository,
  verifyGitConnection,
  type GitCapabilities,
  type GitConnection,
  type GitConnections,
  type GitRepository,
  type GitRepositoryBody,
} from "./api";
import { IconGitBranch } from "./icons";

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

// The derived status: a SHORT state word for the pill, and the sentence that
// explains it as a note under the card's role line. A whole sentence in a pill
// wraps to three lines and runs across the card title (#211), and the
// explanation reads better as body text anyway.
// Deliberately explicit about credential_missing: that state is not a mistake
// the user made, and saying so avoids an afternoon spent re-typing a project
// path that was always correct.
// Typed as possibly-absent on purpose: a status the backend adds later must fall
// back to showing the raw value rather than rendering "undefined".
const STATUS: Record<string, { tone: string; text: string; note?: string } | undefined> = {
  unconfigured: {
    tone: "warn",
    text: "not configured",
    note: "No repositories on this connection yet, so there is nothing for it to reach.",
  },
  credential_missing: {
    tone: "warn",
    text: "no credential",
    note: "No usable credential — the repositories below cannot be reached.",
  },
  configured: { tone: "ok", text: "configured", note: "Not verified yet." },
  verified: { tone: "ok", text: "verified" },
};

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * What to show when a credential write fails.
 *
 * 503 is not a generic failure: it means the deployment holds no encryption key,
 * so NO credential can be stored for any connection until an operator configures
 * one. Nothing was written. Saying that plainly is the difference between "try
 * again" and "this cannot work until someone fixes the deployment".
 */
export function credentialErrorNote(e: unknown): string {
  if (e instanceof ApiError && e.status === 503) {
    return (
      "This deployment has no credential encryption key configured, so credentials " +
      "cannot be stored at all — nothing was written, and retrying will not help. " +
      "An operator has to set CFACTORY_CREDENTIAL_KEY on the backend. Until then " +
      `this connection reads as "no credential". (${e.message})`
    );
  }
  return message(e);
}

function labelsToText(labels: string[]): string {
  return labels.join(", ");
}

function textToLabels(text: string): string[] {
  return text
    .split(",")
    .map((l) => l.trim())
    .filter(Boolean);
}

// ── repository form ─────────────────────────────────────────────────────────
// The same four fields for adding and for editing, each keeping its documented
// meaning, its unset behaviour and a recommendation — the help is the reason the
// panel exists at all, so it does not get shortened for a second use site.

export function RepositoryForm({
  provider,
  initial,
  busy,
  idPrefix,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  provider: string;
  initial?: GitRepository;
  busy: boolean;
  idPrefix: string;
  submitLabel: string;
  onSubmit: (body: GitRepositoryBody) => void;
  onCancel: () => void;
}) {
  const [project, setProject] = useState(initial?.project ?? "");
  const [intake, setIntake] = useState(initial?.intake_project ?? "");
  const [aifactory, setAifactory] = useState(initial?.aifactory_project_id ?? "");
  const [labels, setLabels] = useState(labelsToText(initial?.default_labels ?? []));

  return (
    <div className="set-repo-form">
      <div className="set-field">
        <label className="set-label" htmlFor={`${idPrefix}-project`}>
          Repository
        </label>
        <input
          id={`${idPrefix}-project`}
          className="svc-edit-input mono"
          value={project}
          placeholder={PROJECT_PLACEHOLDER[provider] ?? "owner/repo"}
          onChange={(e) => { setProject(e.target.value); }}
        />
        <span className="set-hint">
          Where a ready card opens its issue. Required — a repository is a project path.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor={`${idPrefix}-intake`}>
          Import from (optional)
        </label>
        <input
          id={`${idPrefix}-intake`}
          className="svc-edit-input mono"
          value={intake}
          placeholder={`defaults to ${project.trim() || "the repository above"}`}
          onChange={(e) => { setIntake(e.target.value); }}
        />
        <span className="set-hint">
          Import existing issues from a different repository than the one above. Empty means
          issues are imported from the repository itself, which is what most boards want.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor={`${idPrefix}-aifactory`}>
          AIFactory project id (optional)
        </label>
        <input
          id={`${idPrefix}-aifactory`}
          className="svc-edit-input mono"
          value={aifactory}
          placeholder="e.g. 5d78d4b9-35f9-4445-92c1-78f3ff60a494"
          onChange={(e) => { setAifactory(e.target.value); }}
        />
        <span className="set-hint">
          The AIFactory project a dispatched card is BUILT in — an AIFactory project UUID, not
          a repository path. Empty means low/medium cards using this repository cannot be
          dispatched and the code and test stages refuse. Builds write real code there, so
          choose it deliberately.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor={`${idPrefix}-labels`}>
          Default labels (optional)
        </label>
        <input
          id={`${idPrefix}-labels`}
          className="svc-edit-input mono"
          value={labels}
          placeholder="comma separated"
          onChange={(e) => { setLabels(e.target.value); }}
        />
        <span className="set-hint">
          Put on issues the board opens in this repository. Empty means none. A
          factory:&lt;tier&gt; label is refused — it is the fleet&apos;s intake trigger and
          would build the same card twice.
        </span>
      </div>

      <div className="set-actions">
        <button
          className="btn btn--sm"
          type="button"
          disabled={busy || !project.trim()}
          onClick={() => {
            onSubmit({
              project: project.trim(),
              intake_project: intake.trim() || null,
              aifactory_project_id: aifactory.trim() || null,
              default_labels: textToLabels(labels),
            });
          }}
        >
          {submitLabel}
        </button>
        <button className="btn btn--sm btn--ghost" type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// ── one repository row ──────────────────────────────────────────────────────

function RepositoryRow({
  repo,
  provider,
  busy,
  actions,
}: {
  repo: GitRepository;
  provider: string;
  busy: boolean;
  actions: Actions;
}) {
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);

  return (
    <div className="set-repo">
      <div className="set-repo-top">
        <span className="set-repo-name mono">{repo.project}</span>
        {repo.is_default ? (
          <span className="set-badge">tenant default</span>
        ) : (
          <button
            className="btn btn--sm btn--ghost"
            type="button"
            disabled={busy}
            title="Cards that name no repository will resolve to this one"
            onClick={() => { actions.makeDefault(repo.id); }}
          >
            Make default
          </button>
        )}
      </div>
      <div className="set-repo-meta">
        <span>imports from {repo.intake_project ?? repo.project}</span>
        <span>
          {repo.aifactory_project_id
            ? `builds in AIFactory project ${repo.aifactory_project_id}`
            : "no AIFactory project — cards here cannot be dispatched"}
        </span>
        {repo.default_labels.length > 0 && (
          <span>labels: {labelsToText(repo.default_labels)}</span>
        )}
      </div>
      <div className="set-repo-actions">
        <button
          className="btn btn--sm btn--ghost"
          type="button"
          disabled={busy}
          onClick={() => { setEditing((v) => !v); }}
        >
          {editing ? "Close" : "Edit"}
        </button>
        {confirming ? (
          <>
            <button
              className="btn btn--sm"
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirming(false);
                actions.removeRepository(repo.id);
              }}
            >
              Confirm remove
            </button>
            <button
              className="btn btn--sm btn--ghost"
              type="button"
              onClick={() => { setConfirming(false); }}
            >
              Keep
            </button>
          </>
        ) : (
          <button
            className="btn btn--sm btn--ghost"
            type="button"
            disabled={busy}
            onClick={() => { setConfirming(true); }}
          >
            Remove
          </button>
        )}
      </div>
      {confirming && (
        <span className="set-hint">
          Cards pointing at this repository are kept — they fall back to the tenant default.
        </span>
      )}
      {editing && (
        <RepositoryForm
          provider={provider}
          initial={repo}
          busy={busy}
          idPrefix={`repo-${String(repo.id)}`}
          submitLabel="Save repository"
          onCancel={() => { setEditing(false); }}
          onSubmit={(body) => {
            setEditing(false);
            actions.updateRepository(repo.id, body);
          }}
        />
      )}
    </div>
  );
}

// ── the install block ───────────────────────────────────────────────────────
// The RFC-0020 §3.4 phase-4 alternative to pasting a token: the human clicks
// through the provider's own consent screen and CFactory ends up holding an
// installation id (GitHub) rather than a credential. It is shown above the paste
// box, not instead of it — Azure DevOps has no install flow, and neither does a
// deployment whose operator has not registered an app.

/** Why an install beats a pasted token, in the one sentence each host allows. */
const INSTALL_PITCH: Record<string, string> = {
  github:
    "Installing the GitHub App lets you choose exactly which repositories it may see, " +
    "and it acts as its own identity — so the audit trail says the app, not you, and " +
    "nothing breaks when you leave. Short-lived tokens are minted per call and never stored.",
  gitlab:
    "Connecting through GitLab's OAuth flow means no token is pasted anywhere. " +
    "Only the refresh credential is kept, encrypted; access tokens are obtained from it " +
    "on demand.",
};

function InstallBlock({
  connection,
  available,
  busy,
  actions,
}: {
  connection: GitConnection;
  available: boolean;
  busy: boolean;
  actions: Actions;
}) {
  const [confirming, setConfirming] = useState(false);
  const install = connection.install;
  const pitch = INSTALL_PITCH[connection.provider];

  // Azure DevOps has no install flow at all (RFC-0020 §3.4), and a deployment
  // that has registered no app cannot start one. Showing a button that can only
  // produce an error would be worse than showing none.
  if (!install && (!available || !pitch)) {
    return null;
  }

  if (!install) {
    return (
      <div className="set-field">
        <label className="set-label">Connect</label>
        <span className="set-hint">{pitch}</span>
        <div className="set-actions">
          <button
            className="btn btn--sm"
            type="button"
            disabled={busy}
            onClick={() => {
              actions.startInstall(connection.id);
            }}
          >
            Connect {PROVIDER_LABEL[connection.provider] ?? connection.provider}
          </button>
        </div>
      </div>
    );
  }

  const degraded = install.status !== "installed";
  return (
    <div className="set-field">
      <label className="set-label">Connected app</label>
      <span className="set-hint">
        {install.account
          ? `Installed on ${install.account}.`
          : "Installed."}{" "}
        {degraded
          ? "The last token refresh FAILED, so this connection cannot reach its repositories " +
            "until it is reconnected. Board writes will fail rather than silently do nothing."
          : "A fresh short-lived token is minted for each call and never stored."}
      </span>
      {degraded && install.error && (
        <div className="set-status-note">last refresh failed: {install.error}</div>
      )}
      <div className="set-actions">
        <button
          className="btn btn--sm"
          type="button"
          disabled={busy}
          onClick={() => {
            actions.startInstall(connection.id);
          }}
        >
          {degraded ? "Reconnect" : "Reconnect or change repositories"}
        </button>
        {confirming ? (
          <>
            <button
              className="btn btn--sm"
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirming(false);
                actions.removeInstall(connection.id);
              }}
            >
              Confirm disconnect
            </button>
            <button
              className="btn btn--sm btn--ghost"
              type="button"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Keep
            </button>
          </>
        ) : (
          <button
            className="btn btn--sm btn--ghost"
            type="button"
            disabled={busy}
            onClick={() => {
              setConfirming(true);
            }}
          >
            Disconnect
          </button>
        )}
      </div>
      {confirming && (
        <span className="set-hint">
          CFactory forgets this install. It does NOT remove the app&apos;s access to your
          repositories — do that on {PROVIDER_LABEL[connection.provider] ?? connection.provider}
          &apos;s own settings page.
        </span>
      )}
    </div>
  );
}

// ── the credential box ──────────────────────────────────────────────────────
// Write-only in both directions: never populated from a response (there is
// nothing to populate it WITH), and emptied the moment the value has been sent,
// so it does not sit in the DOM.

function CredentialField({
  connection,
  busy,
  actions,
}: {
  connection: GitConnection;
  busy: boolean;
  actions: Actions;
}) {
  const [credential, setCredential] = useState("");
  const configured = connection.credential.configured;
  // An install supersedes a pasted credential — the backend resolves it first —
  // so offering "replace the stored credential" here would offer to change
  // something that is no longer being used.
  const installed = connection.install != null;

  return (
    <div className="set-field">
      <label className="set-label" htmlFor={`cred-${String(connection.id)}`}>
        Credential
      </label>
      <div className="set-cred-state">
        {installed ? (
          <span className="set-hint">
            This connection authenticates through its connected app, above. A pasted credential
            is not used while an app is connected.
          </span>
        ) : configured ? (
          <span className="set-hint">
            {connection.credential.source === "tenant"
              ? `Stored for this connection${
                  connection.credential.updated_at
                    ? ` on ${new Date(connection.credential.updated_at).toLocaleDateString()}`
                    : ""
                }${
                  connection.credential.key_version
                    ? ` (key ${connection.credential.key_version})`
                    : ""
                }.`
              : "Using the deployment's environment credential, shared by every tenant."}
          </span>
        ) : (
          <span className="set-hint">No credential stored for this connection.</span>
        )}
      </div>
      <input
        id={`cred-${String(connection.id)}`}
        className="svc-edit-input mono"
        type="password"
        autoComplete="off"
        value={credential}
        placeholder={configured ? "replace the stored credential" : "paste a provider token"}
        onChange={(e) => { setCredential(e.target.value); }}
      />
      <span className="set-hint">
        Encrypted at rest and never shown again — not here, not in any API response. Replacing
        it is the only way to change it; Remove revokes it from this connection. Every use is
        written to the audit trail.
      </span>
      <div className="set-actions">
        <button
          className="btn btn--sm"
          type="button"
          disabled={busy || !credential.trim()}
          onClick={() => {
            const value = credential.trim();
            setCredential("");
            actions.storeCredential(connection.id, value);
          }}
        >
          {configured ? "Replace credential" : "Store credential"}
        </button>
        <button
          className="btn btn--sm btn--ghost"
          type="button"
          disabled={busy || connection.credential.source !== "tenant"}
          title={
            connection.credential.source === "tenant"
              ? undefined
              : "Nothing stored for this connection — the deployment's own credential is set in its environment"
          }
          onClick={() => { actions.removeCredential(connection.id); }}
        >
          Remove
        </button>
      </div>
    </div>
  );
}

// ── the capability matrix, beside the provider selector ─────────────────────

// A SHORT state word per level, matching the status pills elsewhere in the
// panel. The sentence lives under the row, not in the pill.
const LEVEL: Record<string, { tone: string; text: string }> = {
  full: { tone: "ok", text: "works" },
  partial: { tone: "warn", text: "partial" },
  none: { tone: "warn", text: "not available" },
};

/**
 * What picking THIS provider costs (RFC-0020 §3.5).
 *
 * The point of rendering it here rather than only in the docs: the reduction is
 * real, it is not recoverable by configuration, and the moment to learn about it
 * is while choosing the provider — not two days later when a clean build sits on
 * a merge request nothing merges.
 *
 * Reduced-first ordering is deliberate. A tenant reads this to find out what it
 * does NOT get; leading with four green rows buries the answer.
 */
export function CapabilityMatrix({
  provider,
  capabilities,
}: {
  provider: string;
  capabilities: GitCapabilities | null;
}) {
  if (!capabilities) return null;
  const rows = capabilities.capabilities.filter((cap) => cap.support[provider]);
  if (rows.length === 0) return null;
  const reduced = rows.filter((cap) => cap.support[provider] !== "full");
  const works = rows.filter((cap) => cap.support[provider] === "full");

  return (
    <div className="set-caps">
      <span className="set-label">On {PROVIDER_LABEL[provider] ?? provider}</span>
      {reduced.map((cap) => {
        // `rows` already filtered on this key being present; "unknown" is the
        // honest floor rather than a plausible-looking support level (#431).
        const support = cap.support[provider] ?? "unknown";
        const level = LEVEL[support] ?? { tone: "warn", text: support };
        return (
          <div className="set-cap" key={cap.key}>
            <div className="set-cap-head">
              <span className="set-cap-name">{cap.title}</span>
              <span className={`status-pill ${level.tone}`}>
                <span className="dot" /> {level.text}
              </span>
            </div>
            <span className="set-hint">{cap.notes[provider] ?? cap.detail}</span>
          </div>
        );
      })}
      {works.length > 0 && (
        <span className="set-hint">
          Unchanged on this host: {works.map((cap) => cap.title.toLowerCase()).join(", ")}.
        </span>
      )}
      {reduced.length === 0 && (
        <span className="set-hint">Every capability the fleet has is available here.</span>
      )}
    </div>
  );
}

// ── one connection card ─────────────────────────────────────────────────────

export type Actions = {
  updateConnection: (id: number, body: { provider: string; base_url: string | null; label: string }) => void;
  removeConnection: (id: number) => void;
  verify: (id: number) => void;
  storeCredential: (id: number, credential: string) => void;
  removeCredential: (id: number) => void;
  startInstall: (id: number) => void;
  removeInstall: (id: number) => void;
  addRepository: (connectionId: number, body: GitRepositoryBody) => void;
  updateRepository: (id: number, body: GitRepositoryBody) => void;
  removeRepository: (id: number) => void;
  makeDefault: (id: number) => void;
};

/**
 * One git connection: where it points, what it can reach, and its credential.
 *
 * Pure — everything it shows comes from `connection`, and every mutation goes out
 * through `actions`. That is what makes it renderable in a test without a DOM.
 */
export function ConnectionCard({
  connection,
  busy,
  note,
  installAvailable = false,
  actions,
  capabilities,
}: {
  connection: GitConnection;
  busy: boolean;
  // `| undefined` throughout: callers forward these out of lookups
  // (`notes[id]`), which under exactOptionalPropertyTypes is an explicit
  // undefined rather than an omitted prop.
  note?: string | undefined;
  // Whether the DEPLOYMENT has registered an app for this connection's provider.
  // Defaulted false so a cockpit talking to a backend that predates phase 4 shows
  // the paste box, which is all that backend has.
  installAvailable?: boolean | undefined;
  actions: Actions;
  capabilities?: GitCapabilities | null | undefined;
}) {
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [provider, setProvider] = useState(connection.provider);
  const [baseUrl, setBaseUrl] = useState("");
  const [label, setLabel] = useState(connection.label);

  const status = STATUS[connection.status] ?? { tone: "warn", text: connection.status };

  return (
    <div className="svc-card svc-card--code set-card">
      <div className="svc-top">
        <span className="svc-ident">
          <span className="svc-ico">
            <IconGitBranch size={17} />
          </span>
          <span className="svc-name">{connection.label}</span>
        </span>
        <span className={`status-pill ${status.tone}`}>
          <span className="dot" /> {status.text}
        </span>
      </div>
      <div className="svc-role">
        {PROVIDER_LABEL[connection.provider] ?? connection.provider} · {connection.base_url}
      </div>
      {status.note && <div className="set-status-note">{status.note}</div>}
      {connection.verify_error && (
        <div className="set-status-note">last check failed: {connection.verify_error}</div>
      )}
      {note && <div className="set-status-note">{note}</div>}

      <div className="set-field">
        <label className="set-label">Repositories</label>
        {connection.repositories.length === 0 ? (
          <span className="set-hint">
            None yet. Add one so the board has somewhere to open issues through this
            connection.
          </span>
        ) : (
          connection.repositories.map((repo) => (
            <RepositoryRow
              key={repo.id}
              repo={repo}
              provider={connection.provider}
              busy={busy}
              actions={actions}
            />
          ))
        )}
        {adding ? (
          <RepositoryForm
            provider={connection.provider}
            busy={busy}
            idPrefix={`add-${String(connection.id)}`}
            submitLabel="Add repository"
            onCancel={() => { setAdding(false); }}
            onSubmit={(body) => {
              setAdding(false);
              actions.addRepository(connection.id, body);
            }}
          />
        ) : (
          <div className="set-actions">
            <button
              className="btn btn--sm"
              type="button"
              disabled={busy}
              onClick={() => { setAdding(true); }}
            >
              Add repository
            </button>
          </div>
        )}
      </div>

      <InstallBlock
        connection={connection}
        available={installAvailable}
        busy={busy}
        actions={actions}
      />
      <CredentialField connection={connection} busy={busy} actions={actions} />

      {editing && (
        <div className="set-repo-form">
          <div className="set-field">
            <label className="set-label">Provider</label>
            <div className="set-toggle">
              {PROVIDERS.map((p) => (
                <button
                  key={p}
                  className={`set-toggle-btn ${provider === p ? "active" : ""}`}
                  type="button"
                  onClick={() => { setProvider(p); }}
                >
                  {PROVIDER_LABEL[p]}
                </button>
              ))}
            </div>
            <span className="set-hint">
              Changing the provider or the host clears this connection&apos;s verification —
              it proved a connection this one no longer is. The credential is kept.
            </span>
            <CapabilityMatrix provider={provider} capabilities={capabilities ?? null} />
          </div>
          <div className="set-field">
            <label className="set-label" htmlFor={`conn-host-${String(connection.id)}`}>
              Host
            </label>
            <input
              id={`conn-host-${String(connection.id)}`}
              className="svc-edit-input mono"
              value={baseUrl}
              placeholder={`currently ${connection.base_url} — empty keeps the provider default`}
              onChange={(e) => { setBaseUrl(e.target.value); }}
            />
            <span className="set-hint">
              Set this for a self-hosted GitLab, GitHub Enterprise or Azure DevOps Server.
              Empty means the provider&apos;s public host.
            </span>
          </div>
          <div className="set-field">
            <label className="set-label" htmlFor={`conn-label-${String(connection.id)}`}>
              Label
            </label>
            <input
              id={`conn-label-${String(connection.id)}`}
              className="svc-edit-input"
              value={label}
              placeholder="e.g. Work GitHub"
              onChange={(e) => { setLabel(e.target.value); }}
            />
            <span className="set-hint">
              A human name for this connection, shown here and nowhere else. Empty falls back
              to the provider&apos;s name.
            </span>
          </div>
          <div className="set-actions">
            <button
              className="btn btn--sm"
              type="button"
              disabled={busy}
              onClick={() => {
                setEditing(false);
                actions.updateConnection(connection.id, {
                  provider,
                  base_url: baseUrl.trim() || null,
                  label: label.trim(),
                });
              }}
            >
              Save connection
            </button>
            <button
              className="btn btn--sm btn--ghost"
              type="button"
              onClick={() => { setEditing(false); }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="set-actions">
        <button
          className="btn btn--sm"
          type="button"
          disabled={busy}
          onClick={() => { actions.verify(connection.id); }}
          title="Read one of this connection's repositories to prove the host answers and the credential is accepted"
        >
          Verify
        </button>
        <button
          className="btn btn--sm btn--ghost"
          type="button"
          disabled={busy}
          onClick={() => { setEditing((v) => !v); }}
        >
          {editing ? "Close" : "Edit connection"}
        </button>
        {confirming ? (
          <>
            <button
              className="btn btn--sm"
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirming(false);
                actions.removeConnection(connection.id);
              }}
            >
              Confirm remove
            </button>
            <button
              className="btn btn--sm btn--ghost"
              type="button"
              onClick={() => { setConfirming(false); }}
            >
              Keep
            </button>
          </>
        ) : (
          <button
            className="btn btn--sm btn--ghost"
            type="button"
            disabled={busy}
            onClick={() => { setConfirming(true); }}
          >
            Remove
          </button>
        )}
      </div>
      {confirming && (
        <span className="set-hint">
          Removes this connection, its {connection.repositories.length} repositor
          {connection.repositories.length === 1 ? "y" : "ies"} and its credential. Cards are
          kept and fall back to the tenant default.
        </span>
      )}
    </div>
  );
}

// ── the add-a-connection card ───────────────────────────────────────────────

export function AddConnectionCard({
  busy,
  empty,
  onCreate,
  capabilities,
}: {
  busy: boolean;
  empty: boolean;
  onCreate: (body: { provider: string; base_url: string | null; label: string | null }) => void;
  capabilities?: GitCapabilities | null;
}) {
  const [provider, setProvider] = useState<string>("github");
  const [baseUrl, setBaseUrl] = useState("");
  const [label, setLabel] = useState("");

  return (
    <div className="svc-card set-card">
      <div className="svc-top">
        <span className="svc-ident">
          <span className="svc-ico">
            <IconGitBranch size={17} />
          </span>
          <span className="svc-name">Add a connection</span>
        </span>
      </div>
      <div className="svc-role">A git host this board can authenticate to</div>
      {empty && (
        <div className="set-status-note">
          No git connections yet. Until there is one with a repository, cards can neither open
          issues nor be dispatched.
        </div>
      )}

      <div className="set-field">
        <label className="set-label">Provider</label>
        <div className="set-toggle">
          {PROVIDERS.map((p) => (
            <button
              key={p}
              className={`set-toggle-btn ${provider === p ? "active" : ""}`}
              type="button"
              onClick={() => { setProvider(p); }}
            >
              {PROVIDER_LABEL[p]}
            </button>
          ))}
        </div>
        <CapabilityMatrix provider={provider} capabilities={capabilities ?? null} />
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="add-conn-host">
          Host (optional)
        </label>
        <input
          id="add-conn-host"
          className="svc-edit-input mono"
          value={baseUrl}
          placeholder="leave empty for the provider default"
          onChange={(e) => { setBaseUrl(e.target.value); }}
        />
        <span className="set-hint">
          Set this for a self-hosted GitLab, GitHub Enterprise or Azure DevOps Server. One host
          can only be connected once — a second connection to it would leave &quot;which
          credential reaches it?&quot; ambiguous and is refused.
        </span>
      </div>

      <div className="set-field">
        <label className="set-label" htmlFor="add-conn-label">
          Label (optional)
        </label>
        <input
          id="add-conn-label"
          className="svc-edit-input"
          value={label}
          placeholder="e.g. Work GitHub"
          onChange={(e) => { setLabel(e.target.value); }}
        />
        <span className="set-hint">
          A human name for this connection. Empty falls back to the provider&apos;s name.
        </span>
      </div>

      <div className="set-actions">
        <button
          className="btn btn--sm"
          type="button"
          disabled={busy}
          onClick={() => {
            setBaseUrl("");
            setLabel("");
            onCreate({ provider, base_url: baseUrl.trim() || null, label: label.trim() || null });
          }}
        >
          Add connection
        </button>
        <span className="set-hint">
          The credential is stored separately, on the connection, once it exists.
        </span>
      </div>
    </div>
  );
}

/**
 * Settings &gt; Git connections (RFC-0020 §3.3).
 *
 * Which git hosts this tenant's board authenticates to, which repositories it
 * works on through each of them, and which one a card that names none resolves
 * to — the things that used to be environment variables nobody could see from
 * the portal, and that used to be limited to exactly one of each.
 */
export default function GitConnectionsPanel({
  tenant,
  reloadSignal,
}: {
  tenant: string | null;
  reloadSignal: number;
}) {
  const [data, setData] = useState<GitConnections | null>(null);
  // Static per deployment, so it is fetched once and never with the poll.
  const [capabilities, setCapabilities] = useState<GitCapabilities | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // One note per connection: what the last verify or credential write said. Keyed
  // by connection id so a verify on one card cannot narrate on another.
  const [notes, setNotes] = useState<Record<number, string>>({});

  const reload = useCallback(async () => {
    if (!tenant) return;
    const next = await fetchGitConnections(tenant);
    setData(next);
  }, [tenant]);

  useEffect(() => {
    if (!tenant) return;
    let alive = true;
    fetchGitConnections(tenant)
      .then((next) => {
        if (!alive) return;
        setData(next);
        setErr(null);
      })
      .catch((e: unknown) => {
        if (alive) setErr(message(e));
      });
    return () => {
      alive = false;
    };
  }, [tenant, reloadSignal]);

  // The matrix describes the provider layer, not this tenant's configuration, so
  // it is read once and never re-read on a reload signal. A failure is SILENT on
  // purpose: it costs the panel a warning it would have liked to show, and taking
  // the whole connections view down over it would be a far worse trade.
  useEffect(() => {
    if (!tenant) return;
    let alive = true;
    fetchGitCapabilities(tenant)
      .then((next) => {
        if (alive) setCapabilities(next);
      })
      .catch(() => {
        /* the panel still works; it just cannot warn */
      });
    return () => {
      alive = false;
    };
  }, [tenant]);

  // Every mutation is "do it, then re-read the tree": the backend derives
  // `status` and can promote a new default, and a client-side guess at either
  // would be a second source of truth that is wrong exactly when it matters.
  const run = useCallback(
    (work: () => Promise<unknown>, onError: (e: unknown) => string = message) => {
      setBusy(true);
      setErr(null);
      work()
        .then(() => reload())
        .catch((e: unknown) => {
          setErr(onError(e));
        })
        .finally(() => {
          setBusy(false);
        });
    },
    [reload],
  );

  const note = useCallback((id: number, text: string) => {
    setNotes((prev) => ({ ...prev, [id]: text }));
  }, []);

  // Which tenant this cockpit is comes from the backend (it is injected by
  // oauth2-proxy, not chosen by the browser), so until Settings has answered
  // there is nothing to address and nothing to draw.
  if (!tenant) {
    return null;
  }

  const connections = data?.connections ?? [];
  const actions: Actions = {
    updateConnection: (id, body) => {
      run(() => updateGitConnection(tenant, id, body));
    },
    removeConnection: (id) => {
      run(() => deleteGitConnection(tenant, id));
    },
    verify: (id) => {
      run(async () => {
        const result = await verifyGitConnection(tenant, id);
        note(
          id,
          result.ok
            ? `reached ${result.repository ?? result.verified_project ?? "the repository"}`
            : (result.reason ?? "the check failed"),
        );
      });
    },
    storeCredential: (id, credential) => {
      run(async () => {
        await setConnectionCredential(tenant, id, credential);
        note(id, "Credential stored. It cannot be displayed again.");
      }, credentialErrorNote);
    },
    removeCredential: (id) => {
      run(async () => {
        const result = await deleteConnectionCredential(tenant, id);
        note(id, result.removed ? "Credential removed." : "There was no credential to remove.");
      });
    },
    startInstall: (id) => {
      // A full navigation, not a popup: the provider's consent screen is where
      // the human chooses which repositories the app may see, and it must be seen
      // in a real address bar rather than a window that could be spoofing one.
      // The state in this URL is single-use and expires, so following it twice is
      // refused by the callback.
      run(async () => {
        const started = await startGitInstall(tenant, id);
        window.location.assign(started.authorize_url);
      });
    },
    removeInstall: (id) => {
      run(async () => {
        await deleteGitInstall(tenant, id);
        note(
          id,
          "Disconnected. Remove the app's access to your repositories on the provider's own " +
            "settings page — CFactory cannot do that for you.",
        );
      });
    },
    addRepository: (connectionId, body) => {
      run(() => createGitRepository(tenant, connectionId, body));
    },
    updateRepository: (id, body) => {
      run(() => updateGitRepository(tenant, id, body));
    },
    removeRepository: (id) => {
      run(() => deleteGitRepository(tenant, id));
    },
    makeDefault: (id) => {
      run(() => setDefaultGitRepository(tenant, id));
    },
  };

  return (
    <div className="set-section">
      <div className="set-section-head">
        <h2 className="set-section-title">Git connections</h2>
        <p className="set-section-note">
          A connection is a git host this board authenticates to; each one owns the
          repositories it can reach. Exactly one repository is the tenant default — what a card
          that names no repository resolves to, for its issue, its import and its build.
        </p>
      </div>

      {err && <div className="banner banner--error">{err}</div>}

      <div className="set-grid">
        {connections.map((connection) => (
          <ConnectionCard
            key={connection.id}
            connection={connection}
            busy={busy}
            note={notes[connection.id]}
            installAvailable={data?.install_available[connection.provider] ?? false}
            actions={actions}
            capabilities={capabilities}
          />
        ))}
        <AddConnectionCard
          busy={busy}
          empty={connections.length === 0}
          capabilities={capabilities}
          onCreate={(body) => {
            run(() => createGitConnection(tenant, body));
          }}
        />
      </div>
    </div>
  );
}

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import GitConnectionsPanel, {
  AddConnectionCard,
  ConnectionCard,
  RepositoryForm,
  credentialErrorNote,
  type Actions,
} from "./GitConnectionsPanel";
import {
  ApiError,
  GitConnectionsSchema,
  GitCredentialResultSchema,
  GitVerifySchema,
  type GitConnection,
} from "./api";

// Settings > Git connections (RFC-0020 section 3.3, #373). Rendered to static
// markup (react-dom/server, no jsdom — mirrors the other frontend tests), so what
// is under test is the first paint: what each connection shows, and the wire
// contract it parses. The mutation round trips are covered on the backend, where
// the behaviour actually lives.

const NOOP: Actions = {
  updateConnection: () => {},
  removeConnection: () => {},
  verify: () => {},
  storeCredential: () => {},
  removeCredential: () => {},
  addRepository: () => {},
  updateRepository: () => {},
  removeRepository: () => {},
  makeDefault: () => {},
  startInstall: () => {},
  removeInstall: () => {},
};

function repo(id: number, project: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    connection_id: 1,
    tenant_id: "acme",
    project,
    intake_project: null,
    aifactory_project_id: "5d78d4b9-35f9-4445-92c1-78f3ff60a494",
    default_labels: ["board"],
    is_default: false,
    ...extra,
  };
}

// The four states the panel has to survive at once: two providers, two
// repositories each, one connection with no credential, one whose verify failed.
const PAYLOAD = {
  connections: [
    {
      id: 1,
      tenant_id: "acme",
      provider: "github",
      base_url: "https://api.github.com",
      label: "Work GitHub",
      status: "verified",
      credential: {
        configured: true,
        source: "tenant",
        updated_at: "2026-07-20T09:00:00Z",
        key_version: "v1",
      },
      verified_at: "2026-07-26T10:00:00Z",
      verify_error: null,
      repositories: [
        repo(11, "acme/widgets", { is_default: true }),
        repo(12, "acme/gadgets", { intake_project: "acme/legacy-tracker" }),
      ],
    },
    {
      id: 2,
      tenant_id: "acme",
      provider: "gitlab",
      base_url: "https://gitlab.example.com",
      label: "self-hosted GitLab",
      status: "credential_missing",
      credential: { configured: false, source: "none" },
      repositories: [
        repo(21, "acme/platform", { connection_id: 2, aifactory_project_id: null }),
        repo(22, "acme/infra", { connection_id: 2 }),
      ],
    },
    {
      id: 3,
      tenant_id: "acme",
      provider: "azure_devops",
      base_url: "https://dev.azure.com",
      label: "Azure DevOps",
      status: "credential_missing",
      credential: { configured: true, source: "tenant", key_version: "v1" },
      verify_error: "HTTPStatusError: 401 Unauthorized",
      repositories: [repo(31, "acme/proj/repo", { connection_id: 3 })],
    },
  ],
  default_repository_id: 11,
};

function parsed(): GitConnection[] {
  return GitConnectionsSchema.parse(PAYLOAD).connections;
}

function renderAll(): string {
  return parsed()
    .map((connection) =>
      renderToStaticMarkup(<ConnectionCard connection={connection} busy={false} actions={NOOP} />),
    )
    .join("");
}

describe("git connections wire contract", () => {
  it("parses several connections, each with its own repositories", () => {
    const data = GitConnectionsSchema.parse(PAYLOAD);
    expect(data.connections).toHaveLength(3);
    expect(data.connections.map((c) => c.provider)).toEqual(["github", "gitlab", "azure_devops"]);
    expect(data.connections[0].repositories.map((r) => r.project)).toEqual([
      "acme/widgets",
      "acme/gadgets",
    ]);
    expect(data.default_repository_id).toBe(11);
  });

  it("parses a tenant that has configured nothing, which is a state and not an error", () => {
    const data = GitConnectionsSchema.parse({ connections: [], default_repository_id: null });
    expect(data.connections).toEqual([]);
    expect(data.default_repository_id).toBeNull();
  });

  it("defaults the credential block, so an older backend still renders", () => {
    const data = GitConnectionsSchema.parse({
      connections: [
        {
          id: 9,
          tenant_id: "acme",
          provider: "github",
          base_url: "https://api.github.com",
          label: "GitHub",
          status: "credential_missing",
        },
      ],
    });
    expect(data.connections[0].credential.configured).toBe(false);
    expect(data.connections[0].repositories).toEqual([]);
  });

  it("parses a failed verify without a repository", () => {
    const verify = GitVerifySchema.parse({ ok: false, reason: "HTTPStatusError: 404" });
    expect(verify.ok).toBe(false);
    expect(verify.reason).toContain("404");
  });
});

// RFC-0020 section 3.4: the credential is write-only, so the contract the cockpit
// parses has no field that could carry one.
describe("credential wire contract", () => {
  it("carries the masked indicator and nothing else", () => {
    const result = GitCredentialResultSchema.parse({
      ok: true,
      connection_id: 1,
      credential: { configured: true, source: "tenant", key_version: "v1", updated_at: null },
    });
    expect(Object.keys(result.credential).sort()).toEqual([
      "configured",
      "key_version",
      "source",
      "updated_at",
    ]);
  });

  it("drops anything credential-shaped a backend might wrongly send", () => {
    const result = GitCredentialResultSchema.parse({
      ok: true,
      removed: false,
      credential: { configured: true, source: "tenant", token: "glpat-LEAKED" },
    });
    expect(JSON.stringify(result)).not.toContain("glpat-LEAKED");
  });
});

describe("ConnectionCard", () => {
  it("renders every connection, its host and all of its repositories", () => {
    const html = renderAll();
    for (const text of [
      "Work GitHub",
      "GitHub",
      "https://api.github.com",
      "self-hosted GitLab",
      "GitLab",
      "https://gitlab.example.com",
      "Azure DevOps",
      "acme/widgets",
      "acme/gadgets",
      "acme/platform",
      "acme/infra",
      "acme/proj/repo",
    ]) {
      expect(html).toContain(text);
    }
  });

  it("marks exactly one repository as the tenant default", () => {
    const html = renderAll();
    expect(html.match(/tenant default/g)).toHaveLength(1);
    expect(html).toContain("Make default");
  });

  it("says a connection has no credential rather than showing a green state", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard connection={parsed()[1]} busy={false} actions={NOOP} />,
    );
    expect(html).toContain("no credential");
    expect(html).toContain("No credential stored for this connection.");
    expect(html).toContain("Store credential");
  });

  it("keeps a long verify failure in the card body, not in the status pill", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard connection={parsed()[2]} busy={false} actions={NOOP} />,
    );
    // The pill says the short state word; the sentence is a body note (#211).
    expect(html).toContain('class="status-pill warn"><span class="dot"></span> no credential');
    expect(html).toContain(
      '<div class="set-status-note">last check failed: HTTPStatusError: 401 Unauthorized</div>',
    );
  });

  it("names the AIFactory project a repository builds in, and says when there is none", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard connection={parsed()[0]} busy={false} actions={NOOP} />,
    );
    expect(html).toContain("builds in AIFactory project 5d78d4b9-35f9-4445-92c1-78f3ff60a494");
    const noneHtml = renderToStaticMarkup(
      <ConnectionCard connection={parsed()[1]} busy={false} actions={NOOP} />,
    );
    expect(noneHtml).toContain("no AIFactory project — cards here cannot be dispatched");
  });

  it("offers a write-only credential box that never renders a value", () => {
    const html = renderAll();
    expect(html).toContain('type="password"');
    // A password input with no value: there is nothing to populate it with, and
    // the panel is never sent anything that could.
    expect(html).not.toMatch(/type="password"[^>]*value="[^"]/);
  });

  it("never renders a credential, whatever a backend puts in the payload", () => {
    const leaky = GitConnectionsSchema.parse({
      connections: [
        {
          ...PAYLOAD.connections[0],
          token: "ghp_LEAKED",
          credential: { configured: true, source: "tenant", token: "ghp_LEAKED" },
        },
      ],
    }).connections[0];
    const html = renderToStaticMarkup(
      <ConnectionCard connection={leaky} busy={false} actions={NOOP} />,
    );
    expect(html).not.toContain("ghp_LEAKED");
  });
});

// The install flow (RFC-0020 section 3.4 phase 4, #365). What the panel must get
// right is which of the two paths it offers and what it says about a broken one —
// the flow itself lives on the backend, where it is tested end to end.
describe("the install block", () => {
  function withInstall(overrides: Record<string, unknown> | null, provider = "github") {
    return GitConnectionsSchema.parse({
      connections: [{ ...PAYLOAD.connections[0], provider, install: overrides }],
    }).connections[0];
  }

  it("offers to connect an app when the deployment has registered one", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard
        connection={withInstall(null)}
        busy={false}
        installAvailable
        actions={NOOP}
      />,
    );
    expect(html).toContain("Connect GitHub");
    expect(html).toContain("choose exactly which repositories");
    // The paste box stays: a self-hosted operator with no App registered still
    // needs it, and so does Azure DevOps.
    expect(html).toContain('type="password"');
  });

  it("offers nothing when the deployment has registered no app", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard connection={withInstall(null)} busy={false} actions={NOOP} />,
    );
    // A button that can only produce an error is worse than no button.
    expect(html).not.toContain("Connect GitHub");
    expect(html).toContain('type="password"');
  });

  it("never offers an install for azure_devops, which has no install flow", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard
        connection={withInstall(null, "azure_devops")}
        busy={false}
        installAvailable
        actions={NOOP}
      />,
    );
    expect(html).not.toContain("Connect Azure DevOps");
    expect(html).toContain('type="password"');
  });

  it("names the account an app landed on, so a human can confirm it", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard
        connection={withInstall({ provider: "github", installation_id: "4242", account: "acme-org", status: "installed" })}
        busy={false}
        installAvailable
        actions={NOOP}
      />,
    );
    expect(html).toContain("Installed on acme-org.");
    expect(html).toContain("minted for each call and never stored");
    expect(html).toContain("Disconnect");
  });

  it("surfaces a failed refresh instead of showing a connected app as fine", () => {
    const html = renderToStaticMarkup(
      <ConnectionCard
        connection={withInstall({
          provider: "gitlab",
          account: "acme-group",
          status: "credential_missing",
          error: "InstallError: GitLab refused the token request (401)",
        })}
        busy={false}
        installAvailable
        actions={NOOP}
      />,
    );
    expect(html).toContain("last refresh failed: InstallError: GitLab refused");
    expect(html).toContain("Board writes will fail rather than silently do nothing");
    expect(html).toContain("Reconnect");
  });

  it("carries no credential field, whatever a backend puts in the install block", () => {
    const leaky = GitConnectionsSchema.parse({
      connections: [
        {
          ...PAYLOAD.connections[0],
          install: {
            provider: "github",
            status: "installed",
            installation_id: "4242",
            token: "ghs_LEAKED",
            private_key: "-----BEGIN RSA PRIVATE KEY-----",
          },
        },
      ],
    }).connections[0];
    const html = renderToStaticMarkup(
      <ConnectionCard connection={leaky} busy={false} installAvailable actions={NOOP} />,
    );
    expect(JSON.stringify(leaky.install)).not.toContain("ghs_LEAKED");
    expect(html).not.toContain("ghs_LEAKED");
    expect(html).not.toContain("BEGIN RSA PRIVATE KEY");
  });

  it("defaults install_available, so an older backend just shows the paste box", () => {
    const data = GitConnectionsSchema.parse({ connections: [], default_repository_id: null });
    expect(data.install_available).toEqual({});
  });
});

// The confusion this whole epic started from: an AIFactory project id is a UUID,
// not a repository path. The form says so on the field itself.
describe("RepositoryForm", () => {
  it("keeps each field's meaning, its unset behaviour and what it is not", () => {
    const html = renderToStaticMarkup(
      <RepositoryForm
        provider="azure_devops"
        busy={false}
        idPrefix="t"
        submitLabel="Add repository"
        onSubmit={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain("AIFactory project id");
    expect(html).toContain("an AIFactory project UUID, not");
    expect(html).toContain("a repository path");
    expect(html).toContain("Empty means low/medium cards using this repository cannot be");
    // The project placeholder is the connection provider's own path shape.
    expect(html).toContain("organization/project/repo");
  });
});

describe("the empty state", () => {
  it("offers all three hosts and says what is missing", () => {
    const html = renderToStaticMarkup(
      <AddConnectionCard busy={false} empty onCreate={() => {}} />,
    );
    expect(html).toContain("GitHub");
    expect(html).toContain("GitLab");
    expect(html).toContain("Azure DevOps");
    expect(html).toContain("No git connections yet");
  });

  it("draws nothing until the backend has said which tenant this is", () => {
    expect(renderToStaticMarkup(<GitConnectionsPanel tenant={null} reloadSignal={0} />)).toBe("");
  });
});

// A 503 on a credential write is the DEPLOYMENT having no encryption key: no
// credential can be stored for anyone until an operator fixes it, and nothing was
// written. Showing "that failed" would send the user round the retry loop.
describe("credentialErrorNote", () => {
  it("explains a 503 as a deployment without an encryption key", () => {
    const note = credentialErrorNote(new ApiError("no credential key is configured", 503));
    expect(note).toContain("cannot be stored at all");
    expect(note).toContain("nothing was written");
    expect(note).toContain("CFACTORY_CREDENTIAL_KEY");
  });

  it("passes any other failure through as it is", () => {
    expect(credentialErrorNote(new ApiError("connection not found", 404))).toBe(
      "connection not found",
    );
    expect(credentialErrorNote(new Error("network down"))).toBe("network down");
  });
});

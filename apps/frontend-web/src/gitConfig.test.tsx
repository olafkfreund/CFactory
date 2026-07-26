import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import GitConfigPanel from "./GitConfigPanel";
import { GitConfigSchema, GitCredentialResultSchema, GitVerifySchema } from "./api";

// Settings > Git integration (RFC-0020 section 3.3). Rendered to static markup
// (react-dom/server, no jsdom — mirrors StageGates.test.tsx), so what is under
// test is the first paint: the derived status the panel shows, and the wire
// contract it parses. The save/verify round trip is covered on the backend,
// where the behaviour actually lives.

describe("git config wire contract", () => {
  it("parses a fully configured tenant", () => {
    const parsed = GitConfigSchema.parse({
      tenant_id: "acme",
      provider: "gitlab",
      base_url: "https://gitlab.example.com",
      project: "acme/widgets",
      intake_project: null,
      aifactory_project_id: "5d78d4b9",
      default_labels: ["board"],
      status: "verified",
      credential: {
        configured: true,
        source: "tenant",
        updated_at: "2026-07-26T09:00:00Z",
        key_version: "v1",
      },
      verified_at: "2026-07-26T10:00:00Z",
      verify_error: null,
      source: "stored",
    });
    expect(parsed.project).toBe("acme/widgets");
    expect(parsed.status).toBe("verified");
    expect(parsed.credential.configured).toBe(true);
  });

  it("parses an unconfigured tenant, which is a state and not an error", () => {
    const parsed = GitConfigSchema.parse({
      tenant_id: "default",
      provider: "github",
      base_url: "https://api.github.com",
      project: null,
      intake_project: null,
      aifactory_project_id: null,
      default_labels: [],
      status: "unconfigured",
      source: "env",
    });
    expect(parsed.project).toBeNull();
    expect(parsed.source).toBe("env");
  });

  it("parses a failed verify without a repository", () => {
    const parsed = GitVerifySchema.parse({ ok: false, reason: "HTTPStatusError: 404" });
    expect(parsed.ok).toBe(false);
    expect(parsed.reason).toContain("404");
  });

  it("defaults the credential block, so an older backend still renders", () => {
    const parsed = GitConfigSchema.parse({
      tenant_id: "default",
      provider: "github",
      base_url: "https://api.github.com",
      project: "acme/widgets",
      intake_project: null,
      aifactory_project_id: null,
      default_labels: [],
      status: "credential_missing",
      source: "stored",
    });
    expect(parsed.credential.configured).toBe(false);
    expect(parsed.credential.source).toBe("none");
  });
});

// RFC-0020 section 3.4: the credential is write-only, so the contract the
// cockpit parses has no field that could carry one.
describe("git credential wire contract", () => {
  it("carries the masked indicator and nothing else", () => {
    const parsed = GitCredentialResultSchema.parse({
      ok: true,
      credential: { configured: true, source: "tenant", key_version: "v1", updated_at: null },
    });
    expect(parsed.credential.configured).toBe(true);
    expect(Object.keys(parsed.credential).sort()).toEqual([
      "configured",
      "key_version",
      "source",
      "updated_at",
    ]);
  });

  it("drops anything credential-shaped a backend might wrongly send", () => {
    const parsed = GitCredentialResultSchema.parse({
      ok: true,
      removed: false,
      credential: { configured: true, source: "tenant", token: "glpat-LEAKED" },
    });
    expect(JSON.stringify(parsed)).not.toContain("glpat-LEAKED");
  });
});

describe("GitConfigPanel", () => {
  it("offers all three hosts, not just GitHub", () => {
    const html = renderToStaticMarkup(<GitConfigPanel tenant="default" reloadSignal={0} />);
    expect(html).toContain("GitHub");
    expect(html).toContain("GitLab");
    expect(html).toContain("Azure DevOps");
  });

  it("says plainly what the AIFactory project id is, since nobody could tell before", () => {
    const html = renderToStaticMarkup(<GitConfigPanel tenant="default" reloadSignal={0} />);
    expect(html).toContain("AIFactory project id");
    expect(html).toContain("not a");
    expect(html).toContain("repository path");
  });

  it("shows the unconfigured status before anything has loaded", () => {
    const html = renderToStaticMarkup(<GitConfigPanel tenant={null} reloadSignal={0} />);
    expect(html).toContain("not configured");
  });

  it("offers a write-only credential box that never renders a value", () => {
    const html = renderToStaticMarkup(<GitConfigPanel tenant="default" reloadSignal={0} />);
    expect(html).toContain("Credential");
    expect(html).toContain("No credential stored");
    // A password input with no value: there is nothing to populate it with, and
    // the panel is never sent anything that could.
    expect(html).toContain('type="password"');
    expect(html).not.toContain('id="git-credential" class="svc-edit-input mono" value=');
  });
});

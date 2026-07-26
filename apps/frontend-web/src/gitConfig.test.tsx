import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import GitConfigPanel from "./GitConfigPanel";
import { GitConfigSchema, GitVerifySchema } from "./api";

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
      verified_at: "2026-07-26T10:00:00Z",
      verify_error: null,
      source: "stored",
    });
    expect(parsed.project).toBe("acme/widgets");
    expect(parsed.status).toBe("verified");
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
});

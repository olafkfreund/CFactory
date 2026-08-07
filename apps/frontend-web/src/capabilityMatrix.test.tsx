import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { at } from "./testHelpers";

import { AddConnectionCard, CapabilityMatrix } from "./GitConnectionsPanel";
import { GitCapabilitiesSchema, type GitCapabilities } from "./api";

// The capability matrix beside the provider selector (RFC-0020 section 3.5, #366).
//
// The acceptance criterion this covers is criterion 5: the limitation has to be
// visible at CONFIGURATION time, not discovered at run time. So what is asserted
// is that a GitLab tenant looking at the provider selector can read "auto-merge is
// not available here" without leaving the panel.
//
// Rendered to static markup (react-dom/server, no jsdom — mirrors the other
// frontend tests): what is under test is the first paint.

// The backend's real response shape, parsed through the same schema the panel
// uses, so a matrix the backend could not actually produce cannot pass here.
const MATRIX: GitCapabilities = GitCapabilitiesSchema.parse({
  providers: ["github", "gitlab", "azure_devops"],
  capabilities: [
    {
      key: "board_sync",
      title: "Board sync and issue import",
      detail: "Cards open, adopt and mirror issues.",
      support: { github: "full", gitlab: "full", azure_devops: "full" },
      notes: {},
    },
    {
      key: "assign_to_user",
      title: "Delegate an issue to a coding agent",
      detail: "Assigning an issue to the host's own autonomous agent.",
      support: { github: "full", gitlab: "partial", azure_devops: "none" },
      notes: {
        gitlab: "Dispatches a GitLab Duo Workflow, which needs a Duo entitlement.",
        azure_devops: "Raises NotImplementedError. Azure DevOps has no coding agent.",
      },
    },
    {
      key: "enable_auto_merge",
      title: "Auto-merge when green",
      detail: "A reviewed, passing PR merges without a human.",
      support: { github: "full", gitlab: "none", azure_devops: "none" },
      notes: {
        gitlab: "Raises NotImplementedError. A person or your CI performs the merge.",
        azure_devops: "Raises NotImplementedError. Completion is manual or CI-driven.",
      },
    },
  ],
});

describe("CapabilityMatrix", () => {
  it("renders for each provider, accounting for every capability", () => {
    // Every capability is either called out as reduced or named in the
    // "unchanged" summary — no host gets a partial picture. Case-insensitive
    // because the summary sentence lowercases the titles mid-sentence.
    for (const provider of MATRIX.providers) {
      const html = renderToStaticMarkup(
        <CapabilityMatrix provider={provider} capabilities={MATRIX} />,
      ).toLowerCase();
      expect(html).toContain("set-caps");
      for (const cap of MATRIX.capabilities) {
        expect(html, `${cap.key} missing for ${provider}`).toContain(cap.title.toLowerCase());
      }
    }
  });

  it("tells a GitLab tenant auto-merge is not available, and why", () => {
    const html = renderToStaticMarkup(<CapabilityMatrix provider="gitlab" capabilities={MATRIX} />);
    expect(html).toContain("Auto-merge when green");
    expect(html).toContain("not available");
    expect(html).toContain("A person or your CI performs the merge");
    // Partial is its own word: calling Duo "not available" would be as wrong as
    // calling it supported.
    expect(html).toContain("partial");
    expect(html).toContain("Duo entitlement");
  });

  it("tells a GitHub tenant nothing is missing rather than showing empty rows", () => {
    const html = renderToStaticMarkup(<CapabilityMatrix provider="github" capabilities={MATRIX} />);
    expect(html).toContain("Every capability the fleet has is available here");
    expect(html).not.toContain("not available");
  });

  it("says what still works, so a reduction does not read as a broken host", () => {
    const html = renderToStaticMarkup(<CapabilityMatrix provider="gitlab" capabilities={MATRIX} />);
    expect(html).toContain("Unchanged on this host");
    expect(html).toContain("board sync and issue import");
  });

  it("keeps the pills to short state words", () => {
    // The #211 rule: a sentence in a pill wraps to three lines and runs across
    // the title next to it. The explanation belongs in the hint underneath.
    const html = renderToStaticMarkup(<CapabilityMatrix provider="gitlab" capabilities={MATRIX} />);
    const pills = [...html.matchAll(/<span class="status-pill [^"]*">.*?<\/span>\s*([^<]*)</g)].map(
      (m) => at(m, 1).trim(),
    );
    expect(pills.length).toBeGreaterThan(0);
    for (const pill of pills) {
      expect(pill.split(/\s+/).length).toBeLessThanOrEqual(2);
    }
  });

  it("renders nothing at all when the matrix could not be fetched", () => {
    // A failed capabilities read costs the panel a warning; it must not cost the
    // user the connection form.
    expect(renderToStaticMarkup(<CapabilityMatrix provider="gitlab" capabilities={null} />)).toBe(
      "",
    );
  });

  it("is on the add-a-connection card, next to the provider selector", () => {
    const html = renderToStaticMarkup(
      <AddConnectionCard busy={false} empty onCreate={() => {}} capabilities={MATRIX} />,
    );
    // The default selection is GitHub, so the full-support summary is what shows
    // until somebody picks another host.
    expect(html).toContain("On GitHub");
    expect(html).toContain("Every capability the fleet has is available here");
  });
});

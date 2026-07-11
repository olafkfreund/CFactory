import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import StageGates from "./StageGates";
import { ServiceStateSchema } from "./api";

// #167 stage-card verdicts. Rendered to static markup (react-dom/server, no
// jsdom — mirrors TraceabilityPanel.test.tsx). The key guarantees:
//   - each new field renders when present and is omitted cleanly when absent;
//   - an old-envelope stage (no extra at all) renders NOTHING;
//   - the wire payloads parse through the tolerant zod schemas (unknown extra
//     keys pass through, missing optional keys are fine).

describe("StageGates (#167)", () => {
  it("renders nothing for an old-envelope stage", () => {
    expect(renderToStaticMarkup(<StageGates extra={undefined} />)).toBe("");
    expect(renderToStaticMarkup(<StageGates extra={{}} />)).toBe("");
    // Pre-#167 annotations alone (access/verification) do not trigger it either.
    expect(
      renderToStaticMarkup(
        <StageGates extra={{ verification: { achieved_level: "VAL-2", claim: "ran" } }} />,
      ),
    ).toBe("");
  });

  it("renders the routing tier chip with its precedence source", () => {
    const html = renderToStaticMarkup(
      <StageGates extra={{ routing: { tier: "economy", tier_source: "policy" } }} />,
    );
    expect(html).toContain("tier: economy");
    expect(html).toContain("Routing tier picked by: policy");
    // Only the tier chip — no gate badges, no votes.
    expect(html).not.toContain("injection scan");
    expect(html).not.toContain("dependency review");
    expect(html).not.toContain("judge votes");
  });

  it("renders a flagged injection scan as a bad badge with the reason", () => {
    const html = renderToStaticMarkup(
      <StageGates
        extra={{ injection_scan: { verdict: "flagged", reason: "override instruction" } }}
      />,
    );
    expect(html).toContain("injection scan: flagged");
    expect(html).toContain("td-gate--bad");
    expect(html).toContain("override instruction");
  });

  it("renders a clean injection scan as an ok badge", () => {
    const html = renderToStaticMarkup(
      <StageGates extra={{ injection_scan: { verdict: "clean" } }} />,
    );
    expect(html).toContain("injection scan: clean");
    expect(html).toContain("td-gate--ok");
  });

  it("renders the dependency review with its finding count", () => {
    const html = renderToStaticMarkup(
      <StageGates
        extra={{
          dependency_review: {
            status: "fail",
            findings: [{ package: "leftpad", severity: "high", reason: "typosquat" }],
          },
        }}
      />,
    );
    expect(html).toContain("dependency review: fail");
    expect(html).toContain("(1 finding)");
    expect(html).toContain("td-gate--bad");
    expect(html).toContain("leftpad");
  });

  it("renders the judge-vote split as an expandable row", () => {
    const html = renderToStaticMarkup(
      <StageGates
        extra={{
          votes: {
            verdict: "pass",
            majority: 2,
            dissent: 1,
            votes: [
              { judge: "sonnet", verdict: "pass" },
              { judge: "opus", verdict: "pass" },
              { judge: "haiku", verdict: "fail" },
            ],
          },
        }}
      />,
    );
    expect(html).toContain("judge votes: 2 majority / 1 dissent (pass)");
    expect(html).toContain("sonnet");
    expect(html).toContain("td-vote--fail");
  });

  it("parses the new fields tolerantly at the wire boundary", () => {
    const state = ServiceStateSchema.parse({
      task_id: "t-1",
      status: "human_review",
      phase: null,
      extra: {
        routing: { tier: "economy", tier_source: "policy", savings_usd: 0.8, unknown_key: 1 },
        injection_scan: { verdict: "flagged", reason: "x", scanner: "v2" },
        dependency_review: { status: "warn", findings: [{ package: "a", cve: "CVE-1" }] },
        votes: { majority: 3, dissent: 0, quorum: 3 },
      },
    });
    expect(state.extra?.routing?.tier).toBe("economy");
    expect(state.extra?.injection_scan?.verdict).toBe("flagged");
    expect(state.extra?.dependency_review?.status).toBe("warn");
    expect(state.extra?.votes?.majority).toBe(3);
    // And an old envelope parses with none of them.
    const old = ServiceStateSchema.parse({ task_id: "t-2", status: "done", phase: null });
    expect(old.extra).toBeUndefined();
  });
});

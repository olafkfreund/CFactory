import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import ArtifactsPanel from "./ArtifactsPanel";
import TraceabilityPanel from "./TraceabilityPanel";
import { ProcessDetailSchema, TraceabilityRowSchema } from "./api";

// RFC-0015 D2 + §3.3 cockpit views. We render to static markup (react-dom/server
// — already a dependency, no jsdom needed) and assert on the HTML, mirroring the
// zero-extra-deps posture of the existing api.test.ts. The key guarantees:
//   - the matrix renders a row per AC with its tests, VAL, and verdict;
//   - an absent/empty matrix degrades to a "not available" note, never a crash
//     and never an empty table;
//   - a non-string field in a (passthrough) row is stringified, never handed to
//     React as a raw object child (the ReadinessPanel evidence-dict bug class);
//   - the readable artifacts render as Markdown, and absent artifacts render
//     nothing.

describe("TraceabilityPanel (RFC-0015 D2)", () => {
  const rows = [
    {
      ac_id: "AC-1",
      ac_text: "User can log in with valid credentials",
      tests: ["tests/test_login.py::test_ok", "tests/test_login.py::test_session"],
      val_level: "VAL-2",
      status: "passed" as const,
    },
    {
      ac_id: "AC-2",
      ac_text: "Invalid login is rejected",
      tests: ["tests/test_login.py::test_bad"],
      val_level: "VAL-2",
      status: "failed" as const,
    },
    {
      ac_id: "AC-3",
      ac_text: "Password reset email is sent",
      tests: [],
      val_level: "VAL-0",
      status: "not_run" as const,
    },
  ];

  it("renders one row per acceptance criterion with tests, VAL and verdict", () => {
    const html = renderToStaticMarkup(<TraceabilityPanel rows={rows} />);
    // A row per AC.
    expect(html.match(/data-testid="trace-row"/g) ?? []).toHaveLength(3);
    // AC ids + text.
    expect(html).toContain("AC-1");
    expect(html).toContain("User can log in with valid credentials");
    // Covering tests rendered.
    expect(html).toContain("tests/test_login.py::test_ok");
    expect(html).toContain("tests/test_login.py::test_session");
    // VAL level + verdicts.
    expect(html).toContain("VAL-2");
    expect(html).toContain("passed");
    expect(html).toContain("failed");
  });

  it("flags an AC with no covering test as a traceability gap", () => {
    const html = renderToStaticMarkup(<TraceabilityPanel rows={rows} />);
    expect(html).toContain("trace-gap");
    expect(html).toContain("no test");
    expect(html).toContain("not run"); // not_run verdict label
  });

  it("degrades to a 'not available' note when the matrix is absent", () => {
    const empty = renderToStaticMarkup(<TraceabilityPanel rows={null} />);
    expect(empty).toContain("td-traceability-empty");
    expect(empty).toContain("not available");
    // No matrix table is rendered.
    expect(empty).not.toContain("trace-matrix");
  });

  it("degrades to a note (not an empty table) for an empty array", () => {
    const html = renderToStaticMarkup(<TraceabilityPanel rows={[]} />);
    expect(html).toContain("td-traceability-empty");
    expect(html).not.toContain("trace-row");
  });

  it("never renders a raw object as a React child (ReadinessPanel bug class)", () => {
    // A passthrough row could carry an object where text is expected; the panel
    // must stringify it rather than throw "Objects are not valid as a React child".
    const weird = [
      {
        ac_id: "AC-X",
        // ac_text intentionally an object to exercise the defensive stringifier.
        ac_text: { nested: "oops" } as unknown as string,
        tests: [{ path: "t" } as unknown as string],
        status: "passed" as const,
      },
    ];
    expect(() => renderToStaticMarkup(<TraceabilityPanel rows={weird} />)).not.toThrow();
    const html = renderToStaticMarkup(<TraceabilityPanel rows={weird} />);
    expect(html).toContain("AC-X");
    expect(html).toContain("oops"); // object stringified, not dropped/crashed
  });
});

describe("TraceabilityRowSchema (boundary)", () => {
  it("parses a minimal row (ac_id + status only)", () => {
    const parsed = TraceabilityRowSchema.parse({ ac_id: "AC-1", status: "not_run" });
    expect(parsed.ac_id).toBe("AC-1");
    expect(parsed.tests).toBeUndefined();
  });

  it("rejects an out-of-contract status", () => {
    expect(() => TraceabilityRowSchema.parse({ ac_id: "AC-1", status: "bogus" })).toThrow();
  });

  it("surfaces traceability + artifacts on the ProcessDetail payload", () => {
    const parsed = ProcessDetailSchema.parse({
      available: true,
      correlation_key: "k",
      artifacts: { spec: "# Spec", plan: "# Plan", tasks: "- [ ] a task" },
      traceability: [{ ac_id: "AC-1", tests: ["t::ok"], val_level: "VAL-2", status: "passed" }],
    });
    expect(parsed.artifacts?.spec).toBe("# Spec");
    expect(parsed.traceability?.[0].ac_id).toBe("AC-1");
  });

  it("tolerates a process payload with neither artifacts nor traceability", () => {
    const parsed = ProcessDetailSchema.parse({ available: false, correlation_key: "k" });
    expect(parsed.artifacts).toBeUndefined();
    expect(parsed.traceability).toBeUndefined();
  });
});

describe("ArtifactsPanel (RFC-0015 §3.3)", () => {
  it("renders the spec/plan/tasks Markdown as docs", () => {
    const html = renderToStaticMarkup(
      <ArtifactsPanel
        artifacts={{
          spec: "# Login spec\n\nThe **user** logs in.",
          plan: "## Plan\n\n- step one",
          tasks: "1. do the thing",
        }}
      />,
    );
    expect(html).toContain("td-artifacts");
    // Markdown is rendered to nodes (heading + bold), not stringified.
    expect(html).toContain("Login spec");
    expect(html).toContain("<strong>user</strong>");
    // Multiple artifacts => tabs.
    expect(html).toContain("artifacts-tab");
    expect(html).toContain("Plan");
    expect(html).toContain("Tasks");
  });

  it("renders the single present artifact without crashing when others are absent", () => {
    const html = renderToStaticMarkup(<ArtifactsPanel artifacts={{ spec: "# Only spec" }} />);
    expect(html).toContain("Only spec");
  });

  it("renders nothing when no artifacts are present", () => {
    expect(renderToStaticMarkup(<ArtifactsPanel artifacts={undefined} />)).toBe("");
    expect(renderToStaticMarkup(<ArtifactsPanel artifacts={{}} />)).toBe("");
    // Blank-only content is treated as absent too.
    expect(renderToStaticMarkup(<ArtifactsPanel artifacts={{ spec: "   " }} />)).toBe("");
  });
});

import { describe, expect, it } from "vitest";

import { attentionFor, attentionList } from "./needsYou";
import { fmtAge } from "./format";
import type { WorkItem } from "./api";

// Needs-you classifier (#148). Pure logic, no DOM. We build minimal work items
// and assert the one-reason-per-item rule, the stalled-wins precedence, and the
// most-actionable-first ordering.
function wi(
  over: Partial<Record<"p" | "a" | "t", string>>,
  liveness?: WorkItem["liveness"],
  key = "1",
): WorkItem {
  const st = (status: string | undefined) => ({ status: status ?? null });
  return {
    correlation_key: key,
    title: `Task ${key}`,
    pfactory: st(over.p),
    aifactory: st(over.a),
    tfactory: st(over.t),
    timeline: [],
    liveness,
  } as unknown as WorkItem;
}

describe("needsYou", () => {
  it("does not flag a healthy flowing or finished item", () => {
    expect(attentionFor(wi({ a: "in_progress" }))).toBeNull();
    expect(attentionFor(wi({ p: "done", a: "done", t: "passed" }))).toBeNull();
  });

  it("flags a PFactory review as a plan approval", () => {
    const a = attentionFor(wi({ p: "human_review" }));
    expect(a?.kind).toBe("approval");
    expect(a?.stage).toBe("plan");
    expect(a?.service).toBe("PFactory");
  });

  it("flags a TFactory review as a review gate (test stage)", () => {
    const a = attentionFor(wi({ p: "done", a: "done", t: "human_review" }));
    expect(a?.kind).toBe("review");
    expect(a?.stage).toBe("test");
  });

  it("stalled wins over an in-progress build", () => {
    const a = attentionFor(
      wi(
        { a: "in_progress" },
        {
          last_activity_age_seconds: 900,
          active_service: "aifactory",
          active_status: "coding",
          stalled: true,
          deadline_seconds: 600,
        },
      ),
    );
    expect(a?.kind).toBe("stalled");
    expect(a?.stage).toBe("code");
    expect(a?.reason).toContain("15m");
  });

  it("orders approvals, then review gates, then stalls", () => {
    const list = attentionList([
      wi(
        { a: "in_progress" },
        {
          last_activity_age_seconds: 900,
          active_service: "aifactory",
          active_status: "coding",
          stalled: true,
          deadline_seconds: 600,
        },
        "stall",
      ),
      wi({ p: "done", a: "done", t: "human_review" }, undefined, "gate"),
      wi({ p: "human_review" }, undefined, "appr"),
    ]);
    expect(list.map((a) => a.kind)).toEqual(["approval", "review", "stalled"]);
  });

  it("formats ages compactly", () => {
    expect(fmtAge(45)).toBe("45s");
    expect(fmtAge(900)).toBe("15m");
    expect(fmtAge(7200)).toBe("2h");
  });
});

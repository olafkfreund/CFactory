import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./LiveAgents", () => ({ default: () => null, AgentTerminal: () => null }));

import { attentionFor, attentionList, lastActivityAge } from "./needsYou";
import NeedsYouView from "./NeedsYouView";
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

  // #167: a review parked by a machine security gate names WHICH gate fired,
  // not just "needs review".
  it("names the injection-scan gate on a flagged review", () => {
    const item = wi({ p: "done", a: "human_review" });
    item.aifactory.extra = {
      injection_scan: { verdict: "flagged", reason: "spec contains an override instruction" },
    };
    const a = attentionFor(item);
    expect(a?.kind).toBe("review");
    expect(a?.reason).toContain("prompt-injection scan");
    expect(a?.reason).toContain("override instruction");
  });

  it("names the dependency-review gate with its finding count", () => {
    const item = wi({ p: "done", a: "done", t: "human_review" });
    item.tfactory.extra = {
      dependency_review: {
        status: "fail",
        findings: [{ package: "leftpad", severity: "high", reason: "typosquat" }],
      },
    };
    const a = attentionFor(item);
    expect(a?.kind).toBe("review");
    expect(a?.reason).toContain("dependency review");
    expect(a?.reason).toContain("1 finding");
  });

  it("keeps the generic review copy when no gate verdict is present", () => {
    const a = attentionFor(wi({ p: "done", a: "done", t: "human_review" }));
    expect(a?.reason).toBe("Verified — awaiting your review before merge.");
  });

  it("does not name a gate on a clean scan / passing dependency review", () => {
    const item = wi({ p: "done", a: "done", t: "human_review" });
    item.tfactory.extra = {
      injection_scan: { verdict: "clean" },
      dependency_review: { status: "pass", findings: [] },
    };
    const a = attentionFor(item);
    expect(a?.reason).toBe("Verified — awaiting your review before merge.");
  });
  it("flags a stalled stage after a completed build, as the backend count does", () => {
    // Same row as tests/test_needs_you.py: a stalled TFactory stage reads
    // "failed" overall, but the top-bar count includes it, so the list must too.
    const a = attentionFor(
      wi(
        { a: "completed", t: "stalled" },
        {
          last_activity_age_seconds: 3600,
          active_service: "tfactory",
          active_status: "stalled",
          stalled: true,
          deadline_seconds: 900,
        },
      ),
    );
    expect(a?.kind).toBe("stalled");
    expect(a?.stage).toBe("test");
  });

  it("computes last activity age from newest timeline event, then updated_at, else unknown age (#576 B16)", () => {
    const now = Date.parse("2026-07-26T12:00:00Z");
    const withTimeline = {
      ...wi({ p: "human_review" }),
      updated_at: "2026-07-20T12:00:00Z",
      timeline: [
        { service: "pfactory", status: "planning", phase: "plan", updated_at: "2026-07-25T12:00:00Z" },
        { service: "pfactory", status: "human_review", phase: "plan", updated_at: "2026-07-26T10:00:00Z" },
      ],
    };
    const age1 = lastActivityAge(withTimeline, now);
    expect(age1.age).toBe("2h");
    expect(age1.older).toBe(false);

    const withUpdatedOnly = {
      ...wi({ p: "human_review" }),
      updated_at: "2026-07-16T12:00:00Z",
    };
    const age2 = lastActivityAge(withUpdatedOnly, now);
    expect(age2.age).toBe("10d");
    expect(age2.older).toBe(true);

    const noTimestamp = wi({ p: "human_review" });
    const age3 = lastActivityAge(noTimestamp, now);
    expect(age3.age).toBe("unknown age");
    expect(age3.older).toBe(false);
  });

  it("renders recent items, collapsed Older group, and Open/Dismiss buttons (#576 B16)", () => {
    const now = Date.parse("2026-07-26T12:00:00Z");
    const fresh = {
      ...wi({ p: "human_review" }, undefined, "fresh-1"),
      updated_at: "2026-07-26T11:00:00Z",
    };
    fresh.pfactory.task_id = "sess-1";
    fresh.pfactory.repo = "acme/widgets";

    const old = {
      ...wi({ p: "done", a: "human_review" }, undefined, "old-1"),
      updated_at: "2026-07-10T11:00:00Z",
    };
    old.aifactory.task_id = "build-1";

    const unknown = wi({ p: "done", a: "done", t: "human_review" }, undefined, "unk-1");

    const html = renderToStaticMarkup(
      createElement(NeedsYouView, {
        items: [fresh, old, unknown],
        progress: {},
        now,
      }),
    );
    expect(html).toContain("60m");
    expect(html).toContain("16d");
    expect(html).toContain("unknown age");
    expect(html).toContain("Older (1)");
    expect(html).not.toContain("<details class=\"ny-older\" open");
    expect(html).not.toContain("session_id=");
    expect(html).not.toContain("task_id=");
    expect(html).toContain("Open");
    expect(html).toContain("Dismiss");
  });
});

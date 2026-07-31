import { describe, expect, it } from "vitest";

import {
  blockingLenses,
  CostRoutingSchema,
  FeedMessageSchema,
  HealthSchema,
  ServiceStateSchema,
  ServiceStatusSchema,
  TokenTotalsSchema,
  WorkItemSchema,
} from "./api";

// These tests pin the HTTP/WS boundary contract (#114): a representative valid
// payload must parse, and a malformed payload must be rejected at the boundary
// (so a renamed/missing field fails loudly here instead of as `undefined` deep
// in a component). They also document the additive/back-compat tolerances.

describe("HealthSchema", () => {
  it("parses a minimal valid /health payload", () => {
    const parsed = HealthSchema.parse({
      status: "ok",
      service: "cfactory",
      version: "0.1.0",
      upstreams: { pfactory: "online" },
    });
    expect(parsed.status).toBe("ok");
    expect(parsed.upstreams.pfactory).toBe("online");
  });

  it("tolerates additive unknown fields (forward-compat)", () => {
    const parsed = HealthSchema.parse({
      status: "ok",
      service: "cfactory",
      version: "0.1.0",
      upstreams: {},
      a_new_field_added_later: true,
    });
    expect(parsed.service).toBe("cfactory");
  });

  it("rejects a payload missing a required field", () => {
    expect(() =>
      HealthSchema.parse({ status: "ok", service: "cfactory", upstreams: {} }),
    ).toThrow();
  });
});

describe("ServiceStatusSchema", () => {
  it("accepts the optional richer status enum", () => {
    const parsed = ServiceStatusSchema.parse({
      name: "pfactory",
      role: "planner",
      url: "http://pf",
      online: true,
      status: "online",
      detail: null,
    });
    expect(parsed.status).toBe("online");
  });

  it("rejects an out-of-contract status value", () => {
    expect(() =>
      ServiceStatusSchema.parse({
        name: "pfactory",
        role: "planner",
        url: "http://pf",
        online: true,
        status: "weird",
      }),
    ).toThrow();
  });
});

describe("WorkItemSchema", () => {
  const serviceState = { task_id: null, status: null, phase: null };

  it("parses a work item with nested service states and timeline", () => {
    const parsed = WorkItemSchema.parse({
      correlation_key: "abc",
      title: "demo",
      pfactory: { ...serviceState, status: "planned" },
      aifactory: serviceState,
      tfactory: serviceState,
      timeline: [{ service: "pfactory", status: "planned", phase: null, updated_at: "t" }],
    });
    expect(parsed.correlation_key).toBe("abc");
    expect(parsed.pfactory.status).toBe("planned");
    expect(parsed.timeline).toHaveLength(1);
  });

  it("rejects when a required nested service state is malformed", () => {
    expect(() =>
      WorkItemSchema.parse({
        correlation_key: "abc",
        title: null,
        pfactory: { status: null }, // missing task_id + phase
        aifactory: serviceState,
        tfactory: serviceState,
        timeline: [],
      }),
    ).toThrow();
  });
});

describe("TokenTotalsSchema", () => {
  it("parses the totals + by_service + by_work_item shape", () => {
    const parsed = TokenTotalsSchema.parse({
      total: { input_tokens: 1, output_tokens: 2, total_tokens: 3, cost_usd: 0.1 },
      by_service: {
        aifactory: {
          input_tokens: 1,
          output_tokens: 2,
          total_tokens: 3,
          cost_usd: 0.1,
          instrumented: true,
        },
      },
      by_work_item: [{ correlation_key: "k", title: null, total_tokens: 3, cost_usd: 0.1 }],
    });
    expect(parsed.total.total_tokens).toBe(3);
    expect(parsed.by_work_item[0].correlation_key).toBe("k");
  });
});

describe("CostRoutingSchema (RFC-0014 #124)", () => {
  it("parses the estimate-vs-actual + routing payload", () => {
    const parsed = CostRoutingSchema.parse({
      correlation_key: "42",
      routing: {
        routing_class: "standard",
        cost_estimate_usd: 2.1,
        cost_ceiling_usd: 5.0,
        budget_mode: "observe",
        runtime: "claude",
        autonomy: "review",
        autonomy_reason: "difficulty medium",
        rationale: "tier=medium floor=sonnet",
        phase_models: { planning: "opus", coding: "sonnet" },
      },
      actual_cost_usd: 2.4,
      actual_total_tokens: 120000,
      actual_by_model: { sonnet: { total_tokens: 120000, cost_usd: 2.4, workers: 1 } },
      estimate_vs_actual: {
        estimate_usd: 2.1,
        ceiling_usd: 5.0,
        actual_usd: 2.4,
        variance_usd: 0.3,
      },
    });
    expect(parsed.routing?.routing_class).toBe("standard");
    expect(parsed.routing?.phase_models?.planning).toBe("opus");
    expect(parsed.estimate_vs_actual.variance_usd).toBe(0.3);
    expect(parsed.actual_by_model.sonnet.cost_usd).toBe(2.4);
  });

  it("tolerates a legacy task: null routing + null metered estimate", () => {
    const parsed = CostRoutingSchema.parse({
      correlation_key: "99",
      routing: null,
      actual_cost_usd: null,
      actual_total_tokens: 50000,
      actual_by_model: {},
      estimate_vs_actual: {
        estimate_usd: null,
        ceiling_usd: null,
        actual_usd: null,
        variance_usd: null,
      },
    });
    expect(parsed.routing).toBeNull();
    expect(parsed.estimate_vs_actual.estimate_usd).toBeNull();
    expect(parsed.actual_total_tokens).toBe(50000);
  });

  it("rejects a payload missing the estimate_vs_actual block", () => {
    expect(() =>
      CostRoutingSchema.parse({
        correlation_key: "x",
        routing: null,
        actual_cost_usd: null,
        actual_total_tokens: 0,
        actual_by_model: {},
      }),
    ).toThrow();
  });
});

describe("ServiceStateSchema routing in extra (RFC-0014 #124)", () => {
  it("parses a plan slice carrying a routing block under extra", () => {
    const parsed = ServiceStateSchema.parse({
      task_id: "p-1",
      status: "planned",
      phase: "plan",
      extra: { routing: { routing_class: "governed", autonomy: "approval" } },
    });
    expect(parsed.extra?.routing?.routing_class).toBe("governed");
    expect(parsed.extra?.routing?.autonomy).toBe("approval");
  });

  it("leaves extra absent when no routing block is present", () => {
    const parsed = ServiceStateSchema.parse({ task_id: null, status: null, phase: null });
    expect(parsed.extra).toBeUndefined();
  });
});

describe("FeedMessageSchema (WS boundary)", () => {
  it("parses a progress frame via the discriminated union", () => {
    const parsed = FeedMessageSchema.parse({
      type: "progress",
      item: {
        correlation_key: "k",
        service: "aifactory",
        phase: "code",
        percent: 50,
        updated_at: "t",
      },
    });
    expect(parsed.type).toBe("progress");
    if (parsed.type === "progress") {
      expect(parsed.item.percent).toBe(50);
    }
  });

  it("rejects an unknown frame type", () => {
    expect(() => FeedMessageSchema.parse({ type: "bogus", item: {} })).toThrow();
  });
});

// #245: the Approve button must be disabled BEFORE the click on a gate-blocked
// plan. Previously the cockpit could not see the review at all, so it rendered
// an enabled button beside a `human_review` badge and the user got a 409.
describe("blockingLenses", () => {
  const review = (over: Record<string, unknown> = {}) => ({
    gates_passed: false,
    threshold: 0.75,
    aggregate_score: 0.94,
    lenses: [
      { lens: "security", score: 0.7, findings: [{ title: "No auth criteria" }] },
      { lens: "clarity", score: 1.0, findings: [] },
    ],
    ...over,
  });

  it("names the lens that blocks, not the ones that pass", () => {
    expect(blockingLenses(review())).toEqual(["security 0.70/0.75"]);
  });

  it("ignores the aggregate — it is recorded for humans, not the test", () => {
    // 0.94 aggregate sits well above the 0.75 threshold and must not clear it.
    expect(blockingLenses(review()).length).toBe(1);
  });

  it("returns nothing when the gates passed", () => {
    expect(blockingLenses(review({ gates_passed: true }))).toEqual([]);
  });

  it("returns nothing when there is no review at all", () => {
    // PFactory does not send it yet: the button must behave exactly as today.
    expect(blockingLenses(undefined)).toEqual([]);
  });

  it("flags a lens that clears the threshold but carries a finding", () => {
    const r = review({
      lenses: [{ lens: "security", score: 0.9, findings: [{ title: "blocking" }] }],
    });
    expect(blockingLenses(r)).toEqual(["security 0.90/0.75"]);
  });
});

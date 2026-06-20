import { describe, expect, it } from "vitest";

import {
  FeedMessageSchema,
  HealthSchema,
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

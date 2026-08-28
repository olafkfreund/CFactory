import { describe, expect, it } from "vitest";

import contract from "./api-contract.json";
import {
  CostRoutingSchema,
  HealthSchema,
  ProgressResponseSchema,
  RollupsSchema,
  ServicesResponseSchema,
  TokenTotalsSchema,
  TokensByWorkerSchema,
  WorkItemSchema,
  WorkItemsResponseSchema,
  WorkerProgressSchema,
} from "./api";

// Backend-drift gate (Factory#1005). `api.ts` is hand-maintained against the
// CFactory FastAPI backend, and until this file existed nothing noticed when the
// backend renamed a route or dropped a response field: the zod schemas would
// have rejected the payload at runtime, in someone's browser, on whichever
// endpoint they happened to open, after the change shipped.
//
// `scripts/dump_api_contract.py` records what the REAL app returns for the
// endpoints below (in-process, seeded temp store, no network) into
// api-contract.json. This replays each recorded body through the SAME schema
// the client parses with, so a renamed / dropped / retyped field is a red
// `npm test` instead of a runtime surprise.
//
// TWO HALVES, and it matters which catches what:
//   * `python scripts/dump_api_contract.py --check` (CI) diffs the recorded
//     file, so route renames, route removals AND backend ADDITIONS go red.
//   * this file catches drift the client would actually REJECT at runtime.
// Most of these schemas are strict `z.object`, which STRIPS unknown keys rather
// than rejecting them — a field the backend starts sending is invisible here by
// construction (that is how the backend sent `ServiceState.repo` while the
// cockpit silently dropped it, Factory#218). Only the golden diff sees that.
//
// NOT COVERED, stated plainly so nobody reads this file as "the client is
// verified". Of the ~80 schemas in api.ts, this gate covers the 10 endpoints
// below and the schemas they reach:
//   * every WRITE endpoint (POST/PUT/PATCH/DELETE) — the cards surface, the
//     RFC-0020 git-connection/credential/install surface, stage actions,
//     copilot settings, service edits.
//   * every WebSocket frame (`/api/ws`, `/api/live-agents/{}/ws`): FeedMessage,
//     and the live-agent frames. Not REST, so no recorded body exists.
//   * `/api/workitems/{k}/process` (ProcessDetail, ProcessGraph, FlowNode,
//     TraceabilityRow, Artifacts) and `/api/anomalies` (Anomaly) — see the
//     dump script's COVERED table for why each is unrecordable rather than
//     merely unwritten.
//   * `/api/audit/*`, `/api/activity`, `/api/live-agents`, `/api/search`,
//     `/api/cards*`, `/api/settings/*`, `/api/provider-health`, `/connect/*`.
// Adding one is: append to COVERED in the dump script, regenerate, add a case
// here. Nothing about the mechanism is per-endpoint.

// Endpoint -> the schema the client really parses that endpoint's body with,
// plus the field paths this recording is claimed to EXERCISE. The paths are the
// point: a body of nulls parses against almost any schema, so "it parsed" is
// only evidence when the fields were actually populated.
const CASES: {
  endpoint: string;
  schema: { parse: (v: unknown) => unknown };
  exercises: string[];
}[] = [
  {
    endpoint: "GET /health",
    schema: HealthSchema,
    exercises: ["status", "service", "version", "multi_tenant", "upstreams.pfactory"],
  },
  {
    endpoint: "GET /api/services",
    schema: ServicesResponseSchema,
    exercises: ["services.0.name", "services.0.role", "services.0.url", "services.0.online"],
  },
  {
    endpoint: "GET /api/workitems",
    schema: WorkItemsResponseSchema,
    exercises: [
      "count",
      "items.0.correlation_key",
      "items.0.aifactory.task_id",
      "items.0.aifactory.status",
      "items.0.aifactory.usage.total_tokens",
      "items.0.aifactory.usage.cost_usd",
      "items.0.aifactory.workers.w1.provider",
      "items.0.aifactory.by_provider.anthropic.total_tokens",
      "items.0.timeline.0.service",
      "items.0.timeline.0.status",
      "items.0.liveness.deadline_seconds",
    ],
  },
  {
    endpoint: "GET /api/workitems/{correlation_key}",
    schema: WorkItemSchema,
    exercises: ["correlation_key", "pfactory.status", "tfactory.status", "timeline.0.task_id"],
  },
  {
    endpoint: "GET /api/rollups",
    schema: RollupsSchema,
    exercises: [
      "total_work_items",
      "total_events",
      "by_stage.plan",
      "by_status.coding",
      "latency.avg_seconds",
    ],
  },
  {
    endpoint: "GET /api/tokens",
    schema: TokenTotalsSchema,
    exercises: [
      "total.input_tokens",
      "total.cost_usd",
      "by_service.aifactory.total_tokens",
      "by_service.aifactory.instrumented",
      "by_work_item.0.correlation_key",
      "by_work_item.0.total_tokens",
    ],
  },
  {
    endpoint: "GET /api/tokens/by_worker",
    schema: TokensByWorkerSchema,
    exercises: [
      "by_provider.anthropic.total_tokens",
      "by_provider.anthropic.workers",
      "by_model.claude-opus-4.cost_usd",
      "by_work_item.0.workers.0.worker_id",
      "by_work_item.0.workers.0.billing_mode",
    ],
  },
  {
    endpoint: "GET /api/progress",
    schema: ProgressResponseSchema,
    exercises: [
      "items.0.correlation_key",
      "items.0.service",
      "items.0.phase",
      "items.0.percent",
      "items.0.updated_at",
    ],
  },
  {
    endpoint: "GET /api/tasks/{correlation_key}/worker-progress",
    schema: WorkerProgressSchema,
    exercises: ["correlation_key", "series.0.ts", "series.0.total_tokens", "series.0.cost_usd"],
  },
  {
    endpoint: "GET /api/tasks/{correlation_key}/cost-routing",
    schema: CostRoutingSchema,
    exercises: [
      "correlation_key",
      "routing.tier",
      "routing.routing_class",
      "routing.phase_models.coding",
      "actual_total_tokens",
      "estimate_vs_actual.estimate_usd",
    ],
  },
];

// The recorded bodies must stay this rich. MEASURED at 710 non-null leaves when
// the gate was written (health 8, services 20, workitems 277, workitem 272,
// rollups 11, tokens 34, by_worker 50, progress 7, worker-progress 8,
// cost-routing 23). Asserted as a FLOOR because a seed that quietly stops
// populating the tree turns this whole file green-over-nothing: every body would
// still "parse", against nothing. Going UP is fine. Going DOWN means investigate
// first — never just lower the number to get green.
const POPULATED_LEAF_FLOOR = 710;

const responses: Record<string, unknown> = contract.responses;

// Count non-null primitive leaves. `null` does not count: a schema field that is
// `.nullable()` accepts null, so a null leaf is evidence of nothing.
function populatedLeaves(value: unknown): number {
  if (value === null || value === undefined) return 0;
  if (Array.isArray(value)) return value.reduce<number>((n, v) => n + populatedLeaves(v), 0);
  if (typeof value === "object")
    return Object.values(value).reduce<number>((n, v) => n + populatedLeaves(v), 0);
  return 1;
}

// Dotted path lookup; numeric segments index arrays. Returns undefined for any
// missing segment, which is what the assertion below treats as "not exercised".
function at(value: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((node, key) => {
    if (node === null || typeof node !== "object") return undefined;
    return (node as Record<string, unknown>)[key];
  }, value);
}

describe("backend contract (Factory#1005)", () => {
  it("records every covered endpoint", () => {
    expect(Object.keys(responses).sort()).toEqual(CASES.map((c) => c.endpoint).sort());
  });

  for (const { endpoint, schema, exercises } of CASES) {
    describe(endpoint, () => {
      const [method = "", path = ""] = endpoint.split(" ");

      it("is still a route the backend serves", () => {
        // The other drift direction: the client keeps calling a path the
        // backend renamed or deleted. `paths` is the FastAPI app's own route
        // table, so this fails on the rename rather than on the 404.
        const methods: string[] | undefined = (contract.paths as Record<string, string[]>)[path];
        expect(methods, `${path} is not in the backend's route table`).toBeDefined();
        expect(methods).toContain(method.toLowerCase());
      });

      it("still parses with the schema the client uses", () => {
        expect(() => schema.parse(responses[endpoint])).not.toThrow();
      });

      it("actually populated the fields it claims to cover", () => {
        const body = responses[endpoint];
        expect(body).toBeTypeOf("object");
        expect(Object.keys(body as object).length).toBeGreaterThan(0);
        for (const path of exercises) {
          expect(at(body, path), `${endpoint} recorded no value at ${path}`).not.toBeUndefined();
          expect(at(body, path), `${endpoint} recorded null at ${path}`).not.toBeNull();
        }
      });
    });
  }

  it("keeps the recordings substantive", () => {
    const total = Object.values(responses).reduce<number>((n, b) => n + populatedLeaves(b), 0);
    expect(total).toBeGreaterThanOrEqual(POPULATED_LEAF_FLOOR);
  });
});

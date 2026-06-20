// Thin client for the CFactory backend. In dev, Vite proxies /health and /api
// to the backend (see vite.config.ts).
//
// HTTP/WS boundary validation (#114): every payload that crosses the network is
// validated with a zod schema at the boundary and the static type is INFERRED
// inward (`z.infer`). This replaces the previous unchecked `as T` casts: a
// malformed/renamed field now fails loudly at parse time (where it's cheap to
// diagnose) instead of surfacing as an `undefined` deep inside a component.
//
// Schemas are deliberately tolerant where the wire contract is additive:
// `.passthrough()`/`.optional()` mirror the back-compat notes that were on the
// hand-written interfaces, so adding a new backend field never breaks the client.

import { z } from "zod";

// --- Health ---------------------------------------------------------------

export const HealthSchema = z
  .object({
    status: z.string(),
    service: z.string(),
    version: z.string(),
    multi_tenant: z.boolean().optional(),
    upstreams: z.record(z.string()),
  })
  .passthrough();
export type Health = z.infer<typeof HealthSchema>;

export const ServiceStatusSchema = z
  .object({
    name: z.string(),
    role: z.string(),
    url: z.string(),
    online: z.boolean(),
    // Richer classification of the upstream probe (added alongside `online`):
    // "online" | "unauthorized" | "offline" | "error". Optional for back-compat.
    status: z.enum(["online", "unauthorized", "offline", "error"]).optional(),
    detail: z.string().nullable().optional(),
  })
  .passthrough();
export type ServiceStatus = z.infer<typeof ServiceStatusSchema>;

// --- Tokens ---------------------------------------------------------------

export const TokenUsageSchema = z.object({
  input_tokens: z.number(),
  output_tokens: z.number(),
  total_tokens: z.number(),
  cost_usd: z.number(),
  model: z.string().nullable().optional(),
});
export type TokenUsage = z.infer<typeof TokenUsageSchema>;

// RFC-0007 (#88): honest access annotation surfaced when a credentialed (VAL-3)
// lane could not run — e.g. access not curated / interactive MFA. Never a secret.
export const AccessAnnotationSchema = z.object({
  val3: z.string().optional(), // "not_run"
  reason: z.string().optional(),
  risk: z.string().optional(),
  blocked: z
    .array(z.object({ resource: z.string().optional(), reason: z.string().optional() }))
    .optional(),
});
export type AccessAnnotation = z.infer<typeof AccessAnnotationSchema>;

// RFC-0006 (#76): the gate-normalized verification block a verifier attaches.
// `achieved_level` is the truth (never an overclaim); `claim` is the honest
// one-liner. Rendered on the test stage so a VAL-2 result never looks "done".
export const VerificationBlockSchema = z.object({
  target_level: z.string().optional(),
  achieved_level: z.string(),
  claim: z.string(),
  levels: z
    .array(z.object({ level: z.string(), status: z.string(), reason: z.string().optional() }))
    .optional(),
});
export type VerificationBlock = z.infer<typeof VerificationBlockSchema>;

// RFC-0014 (#124): the pre-execution cost-aware routing decision PFactory
// attaches to the plan/contract event. `cost_estimate_usd` is null for
// subscription/local runs (no metered $), mirroring the billing-mode display.
// All fields optional so a partial block (or none) round-trips unchanged.
export const RoutingInfoSchema = z.object({
  routing_class: z.string().nullable().optional(),
  cost_estimate_usd: z.number().nullable().optional(),
  cost_ceiling_usd: z.number().nullable().optional(),
  budget_mode: z.string().nullable().optional(),
  runtime: z.string().nullable().optional(),
  policy: z.string().nullable().optional(),
  rationale: z.string().nullable().optional(),
  autonomy: z.string().nullable().optional(),
  autonomy_reason: z.string().nullable().optional(),
  phase_models: z.record(z.string()).optional(),
});
export type RoutingInfo = z.infer<typeof RoutingInfoSchema>;

export const ServiceStateSchema = z.object({
  task_id: z.string().nullable(),
  status: z.string().nullable(),
  phase: z.string().nullable(),
  usage: TokenUsageSchema.nullable().optional(),
  extra: z
    .object({
      access: AccessAnnotationSchema.optional(),
      verification: VerificationBlockSchema.optional(),
      routing: RoutingInfoSchema.optional(),
    })
    .passthrough()
    .optional(),
});
export type ServiceState = z.infer<typeof ServiceStateSchema>;

export const ServiceTokensSchema = z.object({
  input_tokens: z.number(),
  output_tokens: z.number(),
  total_tokens: z.number(),
  cost_usd: z.number(),
  instrumented: z.boolean(),
});
export type ServiceTokens = z.infer<typeof ServiceTokensSchema>;

// Soft budget (RFC-0001 v1.3 P2). Present on a work-item row ONLY when the
// contract set a budget; absent (undefined) on the common case.
export const BudgetInfoSchema = z.object({
  limit_usd: z.number(),
  spent_usd: z.number(),
  exceeded: z.boolean(),
});
export type BudgetInfo = z.infer<typeof BudgetInfoSchema>;

// Billing mode (#96): real dollars only for metered (api/cloud); subscription /
// local cost no dollars (cost_usd is notional/zero) — show tokens + time instead.
export const BillingModeSchema = z.enum(["api", "cloud", "subscription", "local", "unknown"]);
export type BillingMode = z.infer<typeof BillingModeSchema>;

export const BillingByModeSchema = z.object({
  total_tokens: z.number(),
  cost_usd: z.number(),
  duration_ms: z.number(),
});
export type BillingByMode = z.infer<typeof BillingByModeSchema>;

// Per-task billing summary. Present once a task carries a provider breakdown;
// absent on older/in-flight rows (the panel then falls back conservatively).
export const BillingSummarySchema = z.object({
  modes: z.array(BillingModeSchema),
  by_mode: z.record(BillingModeSchema, BillingByModeSchema),
  metered_cost_usd: z.number(), // real $ — api/cloud only
  nonmetered_tokens: z.number(), // subscription/local tokens (shown without $)
  has_metered: z.boolean(), // true → cost is real, may be shown
});
export type BillingSummary = z.infer<typeof BillingSummarySchema>;

export const WorkItemTokenRowSchema = z.object({
  correlation_key: z.string(),
  title: z.string().nullable(),
  total_tokens: z.number(),
  cost_usd: z.number(),
  budget: BudgetInfoSchema.optional(),
  billing: BillingSummarySchema.optional(),
  elapsed_seconds: z.number().optional(),
});
export type WorkItemTokenRow = z.infer<typeof WorkItemTokenRowSchema>;

export const TokenTotalsSchema = z.object({
  total: z.object({
    input_tokens: z.number(),
    output_tokens: z.number(),
    total_tokens: z.number(),
    cost_usd: z.number(),
    // Real metered spend across the fleet (#96). When 0 with has_billing_modes,
    // everything ran on subscription/local → the cockpit hides the Cost stat.
    metered_cost_usd: z.number().optional(),
    has_billing_modes: z.boolean().optional(),
  }),
  by_service: z.record(ServiceTokensSchema),
  by_work_item: z.array(WorkItemTokenRowSchema),
});
export type TokenTotals = z.infer<typeof TokenTotalsSchema>;

export async function fetchTokens(): Promise<TokenTotals> {
  const resp = await fetch("/api/tokens");
  if (!resp.ok) throw new Error(`tokens error: HTTP ${resp.status}`);
  return TokenTotalsSchema.parse(await resp.json());
}

// Per-worker / per-provider breakdown (RFC-0001 v1.3). Additive to /api/tokens.
export const RollupRowSchema = z.object({
  input_tokens: z.number(),
  output_tokens: z.number(),
  total_tokens: z.number(),
  cost_usd: z.number(),
  workers: z.number(),
});
export type RollupRow = z.infer<typeof RollupRowSchema>;

export const WorkerRowSchema = z.object({
  service: z.string(),
  worker_id: z.string(),
  subtask_id: z.string().nullable().optional(),
  agent_phase: z.string().nullable().optional(),
  provider: z.string().nullable().optional(),
  model: z.string().nullable().optional(),
  input_tokens: z.number(),
  output_tokens: z.number(),
  total_tokens: z.number(),
  cost_usd: z.number(),
  duration_ms: z.number(),
});
export type WorkerRow = z.infer<typeof WorkerRowSchema>;

export const TokensByWorkerSchema = z.object({
  by_provider: z.record(RollupRowSchema),
  by_model: z.record(RollupRowSchema),
  by_work_item: z.array(
    z.object({
      correlation_key: z.string(),
      title: z.string().nullable(),
      workers: z.array(WorkerRowSchema),
    }),
  ),
});
export type TokensByWorker = z.infer<typeof TokensByWorkerSchema>;

export async function fetchTokensByWorker(): Promise<TokensByWorker> {
  const resp = await fetch("/api/tokens/by_worker");
  if (!resp.ok) throw new Error(`tokens by_worker error: HTTP ${resp.status}`);
  return TokensByWorkerSchema.parse(await resp.json());
}

// --- RFC-0014: cost-aware routing — estimate vs actual --------------------

// Actual per-model rolled-up spend (a row of the RFC-0001 usage breakdown).
export const ActualModelRowSchema = z.object({
  total_tokens: z.number(),
  cost_usd: z.number(),
  workers: z.number(),
});
export type ActualModelRow = z.infer<typeof ActualModelRowSchema>;

// Pre-execution estimate joined with actual rolled-up spend for one task.
// `routing` is null for a legacy task with no routing block; actuals still come
// through. Metered fields are null for subscription/local runs (no real $).
export const CostRoutingSchema = z.object({
  correlation_key: z.string(),
  routing: RoutingInfoSchema.nullable(),
  actual_cost_usd: z.number().nullable(),
  actual_total_tokens: z.number(),
  actual_by_model: z.record(ActualModelRowSchema),
  billing: BillingSummarySchema.nullable().optional(),
  estimate_vs_actual: z.object({
    estimate_usd: z.number().nullable(),
    ceiling_usd: z.number().nullable(),
    actual_usd: z.number().nullable(),
    variance_usd: z.number().nullable(),
  }),
});
export type CostRouting = z.infer<typeof CostRoutingSchema>;

// Best-effort: the caller swallows failures and renders nothing, so a transient
// error or a 404 (unknown task) never blanks the task detail.
export async function fetchCostRouting(correlationKey: string): Promise<CostRouting> {
  const resp = await fetch(`/api/tasks/${encodeURIComponent(correlationKey)}/cost-routing`);
  if (!resp.ok) throw new Error(`cost-routing error: HTTP ${String(resp.status)}`);
  return CostRoutingSchema.parse(await resp.json());
}

// --- Tier 1.5: per-worker progress heartbeat series ----------------------

// One sample in the dense cumulative-across-workers series (the sparkline feed).
export const ProgressSampleSchema = z.object({
  ts: z.number(),
  total_tokens: z.number(),
  cost_usd: z.number(),
});
export type ProgressSample = z.infer<typeof ProgressSampleSchema>;

export const WorkerProgressSchema = z.object({
  correlation_key: z.string(),
  // Dense, time-ordered cumulative series. Empty when the task has no heartbeat
  // data yet — the stamp then falls back to its stepwise worker-completion series.
  series: z.array(ProgressSampleSchema),
  // Raw per-worker drill-down (worker_id -> points). Unused by the stamp today.
  workers: z.record(z.array(ProgressSampleSchema)),
});
export type WorkerProgress = z.infer<typeof WorkerProgressSchema>;

// Best-effort: the caller swallows failures and keeps the last-good render, so a
// transient error never blanks a running card.
export async function fetchWorkerProgress(correlationKey: string): Promise<WorkerProgress> {
  const resp = await fetch(`/api/tasks/${encodeURIComponent(correlationKey)}/worker-progress`);
  if (!resp.ok) throw new Error(`worker-progress error: HTTP ${resp.status}`);
  return WorkerProgressSchema.parse(await resp.json());
}

export const TimelineEventSchema = z.object({
  service: z.string(),
  status: z.string().nullable(),
  phase: z.string().nullable(),
  updated_at: z.string(),
});
export type TimelineEvent = z.infer<typeof TimelineEventSchema>;

// Liveness signal for a work item (RFC-0008 §3.4, #105): a `stalled` task is one
// hung in a non-terminal stage past the idle deadline — surfaced as an alert.
export const LivenessSchema = z.object({
  last_activity_age_seconds: z.number(),
  active_service: z.string().nullable(),
  active_status: z.string().nullable(),
  stalled: z.boolean(),
  deadline_seconds: z.number(),
});
export type Liveness = z.infer<typeof LivenessSchema>;

export const WorkItemSchema = z.object({
  correlation_key: z.string(),
  title: z.string().nullable(),
  pfactory: ServiceStateSchema,
  aifactory: ServiceStateSchema,
  tfactory: ServiceStateSchema,
  timeline: z.array(TimelineEventSchema),
  updated_at: z.string().nullable().optional(),
  liveness: LivenessSchema.optional(),
});
export type WorkItem = z.infer<typeof WorkItemSchema>;

export async function fetchHealth(): Promise<Health> {
  const resp = await fetch("/health");
  if (!resp.ok) {
    throw new Error(`backend returned HTTP ${resp.status}`);
  }
  return HealthSchema.parse(await resp.json());
}

export async function fetchServices(): Promise<ServiceStatus[]> {
  const resp = await fetch("/api/services");
  if (!resp.ok) throw new Error(`services error: HTTP ${resp.status}`);
  const data = z.object({ services: z.array(ServiceStatusSchema) }).parse(await resp.json());
  return data.services ?? [];
}

// Provider-auth health (#109): per-service config pre-flight — which provider
// credentials are present. `reported=false` → the service doesn't emit it yet
// (unknown, not an alert); `reachable && reported && !any_configured` → alert.
export const ProviderAuthEntrySchema = z.object({
  name: z.string(),
  role: z.string(),
  url: z.string(),
  reachable: z.boolean(),
  reported: z.boolean(),
  any_configured: z.boolean(),
  providers: z.array(z.object({ name: z.string(), configured: z.boolean() })),
  error: z.string().nullable(),
});
export type ProviderAuthEntry = z.infer<typeof ProviderAuthEntrySchema>;

export const ProviderHealthSchema = z.object({
  services: z.array(ProviderAuthEntrySchema),
  alert_count: z.number(),
});
export type ProviderHealth = z.infer<typeof ProviderHealthSchema>;

export async function fetchProviderHealth(): Promise<ProviderHealth> {
  const resp = await fetch("/api/provider-health");
  if (!resp.ok) throw new Error(`provider-health error: HTTP ${resp.status}`);
  return ProviderHealthSchema.parse(await resp.json());
}

export const CopilotSettingsSchema = z.object({
  provider: z.string(), // "claude" | "ollama"
  model: z.string(),
  providers: z.array(z.string()), // selectable providers
  base_url: z.string().optional(),
  has_key: z.boolean().optional(),
  reachable: z.boolean().nullable().optional(), // null = not an OpenAI-compatible provider / no probe
  models: z.array(z.string()).optional(), // available cloud models (when reachable)
  error: z.string().optional(),
});
export type CopilotSettings = z.infer<typeof CopilotSettingsSchema>;

export async function fetchSettings(): Promise<CopilotSettings> {
  const resp = await fetch("/api/settings");
  if (!resp.ok) throw new Error(`settings error: HTTP ${resp.status}`);
  const data = z.object({ copilot: CopilotSettingsSchema }).parse(await resp.json());
  return data.copilot;
}

export async function updateCopilotSettings(
  provider: string,
  model: string,
): Promise<CopilotSettings> {
  const resp = await fetch("/api/settings/copilot", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, model }),
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      detail =
        z.object({ detail: z.string().optional() }).parse(await resp.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return z.object({ copilot: CopilotSettingsSchema }).parse(await resp.json()).copilot;
}

export async function updateService(name: string, url: string): Promise<ServiceStatus> {
  const resp = await fetch(`/api/services/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      detail =
        z.object({ detail: z.string().optional() }).parse(await resp.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return ServiceStatusSchema.parse(await resp.json());
}

export const ActivityEntrySchema = z.object({
  service: z.string(),
  correlation_key: z.string(),
  status: z.string(),
  phase: z.string().nullable().optional(),
  updated_at: z.string(),
  title: z.string().nullable().optional(),
});
export type ActivityEntry = z.infer<typeof ActivityEntrySchema>;

export async function fetchActivity(limit = 50): Promise<ActivityEntry[]> {
  const resp = await fetch(`/api/activity?limit=${limit}`);
  if (!resp.ok) throw new Error(`activity error: HTTP ${resp.status}`);
  const data = z.object({ activity: z.array(ActivityEntrySchema) }).parse(await resp.json());
  return data.activity ?? [];
}

export async function fetchWorkItems(): Promise<WorkItem[]> {
  const resp = await fetch("/api/workitems");
  if (!resp.ok) {
    throw new Error(`backend returned HTTP ${resp.status}`);
  }
  const body = z
    .object({ count: z.number(), items: z.array(WorkItemSchema) })
    .parse(await resp.json());
  return body.items;
}

export const LiveProgressSchema = z.object({
  correlation_key: z.string(),
  service: z.string(),
  phase: z.string().nullable(),
  percent: z.number().nullable(),
  subtask: z.string().nullable().optional(),
  updated_at: z.string(),
});
export type LiveProgress = z.infer<typeof LiveProgressSchema>;

// Live cockpit feed frames (the /api/ws WebSocket boundary). Discriminated on
// `type`; parsed on every inbound frame so a malformed frame is dropped, never
// mis-handled downstream.
export const FeedMessageSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("workitem"), item: WorkItemSchema }),
  z.object({ type: z.literal("snapshot"), items: z.array(WorkItemSchema) }),
  z.object({ type: z.literal("progress"), item: LiveProgressSchema }),
]);
export type FeedMessage = z.infer<typeof FeedMessageSchema>;

export async function fetchProgress(): Promise<LiveProgress[]> {
  const resp = await fetch("/api/progress");
  if (!resp.ok) throw new Error(`progress error: HTTP ${resp.status}`);
  return z.object({ items: z.array(LiveProgressSchema) }).parse(await resp.json()).items;
}

export interface FeedHandle {
  close(): void;
}

// Open the live cockpit feed with auto-reconnect + keepalive. The raw socket
// is intentionally hidden: callers can't accidentally hold a dead reference.
// - Keepalive: a "ping" text frame every 25s keeps proxies (Cloudflare reaps
//   idle WebSockets ~100s) and the backend's receive loop from dropping us.
// - Reconnect: on any close we retry with capped exponential backoff, so a
//   network blip or proxy timeout no longer leaves the cockpit frozen on stale
//   data. onOpen/onClose fire on every (re)connect so the live pill tracks it.
export function openFeed(
  onMessage: (msg: FeedMessage) => void,
  onOpen?: () => void,
  onClose?: () => void,
): FeedHandle {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${window.location.host}/api/ws`;
  let ws: WebSocket | null = null;
  let closedByCaller = false;
  let backoff = 1000;
  let pingTimer: number | undefined;
  let reconnectTimer: number | undefined;

  const connect = () => {
    ws = new WebSocket(url);
    ws.onopen = () => {
      backoff = 1000;
      onOpen?.();
      pingTimer = window.setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) {
          try {
            ws.send("ping");
          } catch {
            /* will surface as a close */
          }
        }
      }, 25_000);
    };
    ws.onmessage = (ev) => {
      try {
        // Validate the frame at the WS boundary; drop anything that doesn't match
        // the contract rather than handing a malformed object to the cockpit.
        onMessage(FeedMessageSchema.parse(JSON.parse(ev.data as string)));
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onerror = () => {
      try {
        ws?.close();
      } catch {
        /* onclose will handle retry */
      }
    };
    ws.onclose = () => {
      window.clearInterval(pingTimer);
      onClose?.();
      if (closedByCaller) return;
      const delay = backoff;
      backoff = Math.min(backoff * 2, 15_000);
      reconnectTimer = window.setTimeout(connect, delay);
    };
  };

  connect();

  return {
    close() {
      closedByCaller = true;
      window.clearInterval(pingTimer);
      window.clearTimeout(reconnectTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    },
  };
}

// --- Live agents (#33/#34): AIFactory rmux consoles proxied by the backend ---

export const LiveAgentSchema = z.object({
  correlation_key: z.string(),
  spec_id: z.string(),
  service: z.string(),
  title: z.string().nullable(),
  phase: z.string().nullable(),
  status: z.string().nullable(),
  // Cockpit-side proxy path the backend re-streams from; never a raw AIFactory URL.
  ws_path: z.string(),
});
export type LiveAgent = z.infer<typeof LiveAgentSchema>;

export const LiveAgentsResponseSchema = z.object({
  rmux_enabled: z.boolean(),
  count: z.number(),
  agents: z.array(LiveAgentSchema),
});
export type LiveAgentsResponse = z.infer<typeof LiveAgentsResponseSchema>;

export async function fetchLiveAgents(): Promise<LiveAgentsResponse> {
  const resp = await fetch("/api/live-agents");
  if (!resp.ok) throw new Error(`live-agents error: HTTP ${resp.status}`);
  return LiveAgentsResponseSchema.parse(await resp.json());
}

// Open one agent's console stream (read-only ANSI bytes for xterm.js). The
// caller writes incoming frames to a terminal and closes the socket on unmount.
export function openAgentConsole(wsPath: string): WebSocket {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const path = wsPath.startsWith("/") ? wsPath : `/${wsPath}`;
  const ws = new WebSocket(`${proto}://${window.location.host}${path}`);
  ws.binaryType = "arraybuffer";
  return ws;
}

// --- Task process detail (#45): rich live state for one work item's task ---

export const ProcessSubtaskSchema = z.object({
  title: z.string().nullable(),
  status: z.string().nullable(),
});
export type ProcessSubtask = z.infer<typeof ProcessSubtaskSchema>;

// --- Live execution graph (#94): the animated DAG behind the task detail ---
//
// A stage (plan/code/test) exposes its work as a list of nodes with dependency
// edges; the cockpit lays them out as wave-columns and animates them live. The
// contract is deliberately minimal + additive — a node only needs an id, a
// label and a raw status; everything else (deps for edges, the timestamps for
// per-node timers, kind for the accent colour) is optional and the diagram
// degrades gracefully when a producer can't supply it yet.

export const FlowNodeSchema = z.object({
  id: z.string(),
  label: z.string(),
  // Domain tag used only for the accent dot: PFactory child kind
  // (feature/testing/cicd/infra/docs), a TFactory lane name, or a phase.
  kind: z.string().nullable().optional(),
  // Raw status string from the producer; classified client-side (see taskFlow.ts)
  // into pending | active | done | failed | stalled.
  status: z.string().nullable().optional(),
  started_at: z.string().nullable().optional(),
  completed_at: z.string().nullable().optional(),
  // IDs of prerequisite nodes — each becomes an edge dep → node.
  deps: z.array(z.string()).optional(),
  // Optional producer-computed column; when absent it's derived from deps.
  wave: z.number().nullable().optional(),
});
export type FlowNode = z.infer<typeof FlowNodeSchema>;

const StageSchema = z.enum(["plan", "code", "test"]);

export const ProcessGraphSchema = z.object({
  stage: StageSchema,
  nodes: z.array(FlowNodeSchema),
  title: z.string().nullable().optional(),
});
export type ProcessGraph = z.infer<typeof ProcessGraphSchema>;

export const ProcessDetailSchema = z.object({
  available: z.boolean(),
  correlation_key: z.string(),
  service: z.string().optional(),
  task_id: z.string().nullable().optional(),
  title: z.string().nullable().optional(),
  status: z.string().nullable().optional(),
  phase: z.string().nullable().optional(),
  reason: z.string().optional(),
  progress: z
    .object({
      phase: z.string().nullable(),
      phase_percent: z.number().nullable(),
      overall_percent: z.number().nullable(),
      current_subtask: z.string().nullable(),
      message: z.string().nullable(),
    })
    .optional(),
  subtasks: z.array(ProcessSubtaskSchema).optional(),
  // The live execution DAG for this stage (#94). Best-effort + additive: absent
  // on older builds / producers, in which case the diagram simply doesn't render.
  // `graph` is the furthest stage (default view); `graphs` carries every available
  // stage so the modal can switch between plan / code / test.
  graph: ProcessGraphSchema.nullable().optional(),
  graphs: z.record(StageSchema, ProcessGraphSchema).optional(),
  branch: z.string().nullable().optional(),
  updated_at: z.string().nullable().optional(),
  // Browser-lane test evidence captured by TFactory: screenshot + recording file
  // names. CFactory proxies the bytes (see evidenceMediaUrl) so they render
  // same-origin. Absent when the task produced none.
  evidence: z
    .object({
      spec_id: z.string(),
      screenshots: z.array(z.string()),
      videos: z.array(z.string()),
    })
    .nullable()
    .optional(),
});
export type ProcessDetail = z.infer<typeof ProcessDetailSchema>;

export async function fetchProcess(correlationKey: string): Promise<ProcessDetail> {
  const resp = await fetch(`/api/workitems/${encodeURIComponent(correlationKey)}/process`);
  if (!resp.ok) throw new Error(`process error: HTTP ${resp.status}`);
  return ProcessDetailSchema.parse(await resp.json());
}

/**
 * URL for one piece of browser-lane evidence, proxied through CFactory (so it's
 * same-origin and authenticated by the cockpit session). Use directly as an
 * `<img src>` / `<video src>`. `kind` ∈ {screenshots, videos}.
 */
export function evidenceMediaUrl(
  correlationKey: string,
  kind: "screenshots" | "videos",
  name: string,
): string {
  return `/api/workitems/${encodeURIComponent(correlationKey)}/evidence/${kind}/${encodeURIComponent(name)}`;
}

export const AnomalySchema = z.object({
  kind: z.string(),
  severity: z.string(),
  correlation_key: z.string(),
  title: z.string().nullable(),
  detail: z.string(),
});
export type Anomaly = z.infer<typeof AnomalySchema>;

export const RollupsSchema = z.object({
  total_work_items: z.number(),
  by_stage: z.object({ plan: z.number(), code: z.number(), test: z.number() }),
  total_events: z.number(),
  latency: z.object({ avg_seconds: z.number(), max_seconds: z.number() }).nullable(),
});
export type Rollups = z.infer<typeof RollupsSchema>;

export async function askCopilot(question: string): Promise<string> {
  const resp = await fetch("/api/copilot/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!resp.ok) throw new Error(`copilot error: HTTP ${resp.status}`);
  const body = z.object({ answer: z.string() }).parse(await resp.json());
  return body.answer;
}

export async function fetchAnomalies(): Promise<Anomaly[]> {
  const resp = await fetch("/api/anomalies");
  if (!resp.ok) throw new Error(`anomalies error: HTTP ${resp.status}`);
  return z.object({ anomalies: z.array(AnomalySchema) }).parse(await resp.json()).anomalies;
}

export async function fetchRollups(): Promise<Rollups> {
  const resp = await fetch("/api/rollups");
  if (!resp.ok) throw new Error(`rollups error: HTTP ${resp.status}`);
  return RollupsSchema.parse(await resp.json());
}

export const ActionStepSchema = z.object({
  method: z.string(),
  endpoint: z.string(),
  payload: z.record(z.unknown()).optional(),
});
export type ActionStep = z.infer<typeof ActionStepSchema>;

export const PreparedActionSchema = z.object({
  kind: z.string(),
  correlation_key: z.string(),
  target_service: z.string(),
  method: z.string(),
  endpoint: z.string(),
  payload: z.record(z.unknown()),
  follow_ups: z.array(ActionStepSchema).optional(),
  rationale: z.string(),
});
export type PreparedAction = z.infer<typeof PreparedActionSchema>;

export const ExecuteResultSchema = z.object({
  status_code: z.number(),
  ok: z.boolean(),
  body: z.unknown().optional(),
  error: z.string().optional(),
  steps: z
    .array(
      z.object({
        method: z.string(),
        endpoint: z.string(),
        status_code: z.number(),
        ok: z.boolean(),
      }),
    )
    .optional(),
});
export type ExecuteResult = z.infer<typeof ExecuteResultSchema>;

export const AuditEntrySchema = z.object({
  id: z.number(),
  ts: z.string(),
  actor: z.string(),
  kind: z.string(),
  correlation_key: z.string(),
  target_service: z.string(),
  endpoint: z.string(),
  status_code: z.number(),
  ok: z.boolean(),
});
export type AuditEntry = z.infer<typeof AuditEntrySchema>;

// Build (but do NOT execute) a PreparedAction. Advise-only — no upstream write.
export async function proposeAction(
  kind: string,
  correlation_key: string,
  note?: string,
): Promise<PreparedAction> {
  const resp = await fetch("/api/actions/propose", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, correlation_key, note }),
  });
  if (resp.status === 404) throw new Error("no actionable task for this item");
  if (!resp.ok) throw new Error(`propose failed: HTTP ${resp.status}`);
  return PreparedActionSchema.parse(await resp.json());
}

// Execute a CONFIRMED PreparedAction — the explicit human write step.
export async function executeAction(action: PreparedAction): Promise<ExecuteResult> {
  const resp = await fetch("/api/actions/execute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action),
  });
  if (!resp.ok) throw new Error(`execute failed: HTTP ${resp.status}`);
  return ExecuteResultSchema.parse(await resp.json());
}

export async function fetchAudit(): Promise<AuditEntry[]> {
  const resp = await fetch("/api/audit");
  if (!resp.ok) throw new Error(`audit error: HTTP ${resp.status}`);
  return z
    .object({ count: z.number(), entries: z.array(AuditEntrySchema) })
    .parse(await resp.json()).entries;
}

export const ConnectTokenSchema = z.object({
  // The CFactory API token to use from an editor / external client. Empty in
  // OPEN mode (no CFACTORY_API_KEYS configured).
  token: z.string(),
  configured: z.boolean(),
  // Public base URL of the token-gated API surface (the editor points here).
  // Empty when not configured (CFACTORY_PUBLIC_API_URL unset).
  connect_url: z.string(),
});
export type ConnectToken = z.infer<typeof ConnectTokenSchema>;

// The API token shown on the /settings/token copy page (#73). Guarded behind the
// same gate as the rest of the cockpit UI.
export async function fetchConnectToken(): Promise<ConnectToken> {
  const resp = await fetch("/api/settings/token");
  if (!resp.ok) throw new Error(`token error: HTTP ${resp.status}`);
  return ConnectTokenSchema.parse(await resp.json());
}

// Best-effort poll of all upstream services; returns the per-service summary.
export async function refresh(): Promise<Record<string, unknown>> {
  const resp = await fetch("/api/refresh", { method: "POST" });
  if (!resp.ok) {
    throw new Error(`refresh failed: HTTP ${resp.status}`);
  }
  const body = z.object({ refreshed: z.record(z.unknown()) }).parse(await resp.json());
  return body.refreshed;
}

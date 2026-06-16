// Thin client for the CFactory backend. In dev, Vite proxies /health and /api
// to the backend (see vite.config.ts).

export interface Health {
  status: string;
  service: string;
  version: string;
  multi_tenant?: boolean;
  upstreams: Record<string, string>;
}

export interface ServiceStatus {
  name: string;
  role: string;
  url: string;
  online: boolean;
  // Richer classification of the upstream probe (added alongside `online`):
  // "online" | "unauthorized" | "offline" | "error". Optional for back-compat.
  status?: "online" | "unauthorized" | "offline" | "error";
  detail?: string | null;
}

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  model?: string | null;
}

// RFC-0007 (#88): honest access annotation surfaced when a credentialed (VAL-3)
// lane could not run — e.g. access not curated / interactive MFA. Never a secret.
export interface AccessAnnotation {
  val3?: string; // "not_run"
  reason?: string;
  risk?: string;
  blocked?: { resource?: string; reason?: string }[];
}

export interface ServiceState {
  task_id: string | null;
  status: string | null;
  phase: string | null;
  usage?: TokenUsage | null;
  extra?: { access?: AccessAnnotation } & Record<string, unknown>;
}

export interface ServiceTokens {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  instrumented: boolean;
}

// Soft budget (RFC-0001 v1.3 P2). Present on a work-item row ONLY when the
// contract set a budget; absent (undefined) on the common case.
export interface BudgetInfo {
  limit_usd: number;
  spent_usd: number;
  exceeded: boolean;
}

// Billing mode (#96): real dollars only for metered (api/cloud); subscription /
// local cost no dollars (cost_usd is notional/zero) — show tokens + time instead.
export type BillingMode = "api" | "cloud" | "subscription" | "local" | "unknown";

export interface BillingByMode {
  total_tokens: number;
  cost_usd: number;
  duration_ms: number;
}

// Per-task billing summary. Present once a task carries a provider breakdown;
// absent on older/in-flight rows (the panel then falls back conservatively).
export interface BillingSummary {
  modes: BillingMode[];
  by_mode: Partial<Record<BillingMode, BillingByMode>>;
  metered_cost_usd: number; // real $ — api/cloud only
  nonmetered_tokens: number; // subscription/local tokens (shown without $)
  has_metered: boolean; // true → cost is real, may be shown
}

export interface WorkItemTokenRow {
  correlation_key: string;
  title: string | null;
  total_tokens: number;
  cost_usd: number;
  budget?: BudgetInfo;
  billing?: BillingSummary;
  elapsed_seconds?: number;
}

export interface TokenTotals {
  total: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cost_usd: number;
    // Real metered spend across the fleet (#96). When 0 with has_billing_modes,
    // everything ran on subscription/local → the cockpit hides the Cost stat.
    metered_cost_usd?: number;
    has_billing_modes?: boolean;
  };
  by_service: Record<string, ServiceTokens>;
  by_work_item: WorkItemTokenRow[];
}

export async function fetchTokens(): Promise<TokenTotals> {
  const resp = await fetch("/api/tokens");
  if (!resp.ok) throw new Error(`tokens error: HTTP ${resp.status}`);
  return (await resp.json()) as TokenTotals;
}

// Per-worker / per-provider breakdown (RFC-0001 v1.3). Additive to /api/tokens.
export interface RollupRow {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  workers: number;
}

export interface WorkerRow {
  service: string;
  worker_id: string;
  subtask_id?: string | null;
  agent_phase?: string | null;
  provider?: string | null;
  model?: string | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  duration_ms: number;
}

export interface TokensByWorker {
  by_provider: Record<string, RollupRow>;
  by_model: Record<string, RollupRow>;
  by_work_item: { correlation_key: string; title: string | null; workers: WorkerRow[] }[];
}

export async function fetchTokensByWorker(): Promise<TokensByWorker> {
  const resp = await fetch("/api/tokens/by_worker");
  if (!resp.ok) throw new Error(`tokens by_worker error: HTTP ${resp.status}`);
  return (await resp.json()) as TokensByWorker;
}

// --- Tier 1.5: per-worker progress heartbeat series ----------------------

// One sample in the dense cumulative-across-workers series (the sparkline feed).
export interface ProgressSample {
  ts: number;
  total_tokens: number;
  cost_usd: number;
}

export interface WorkerProgress {
  correlation_key: string;
  // Dense, time-ordered cumulative series. Empty when the task has no heartbeat
  // data yet — the stamp then falls back to its stepwise worker-completion series.
  series: ProgressSample[];
  // Raw per-worker drill-down (worker_id -> points). Unused by the stamp today.
  workers: Record<string, ProgressSample[]>;
}

// Best-effort: the caller swallows failures and keeps the last-good render, so a
// transient error never blanks a running card.
export async function fetchWorkerProgress(correlationKey: string): Promise<WorkerProgress> {
  const resp = await fetch(`/api/tasks/${encodeURIComponent(correlationKey)}/worker-progress`);
  if (!resp.ok) throw new Error(`worker-progress error: HTTP ${resp.status}`);
  return (await resp.json()) as WorkerProgress;
}

export interface TimelineEvent {
  service: string;
  status: string | null;
  phase: string | null;
  updated_at: string;
}

export interface WorkItem {
  correlation_key: string;
  title: string | null;
  pfactory: ServiceState;
  aifactory: ServiceState;
  tfactory: ServiceState;
  timeline: TimelineEvent[];
}

export async function fetchHealth(): Promise<Health> {
  const resp = await fetch("/health");
  if (!resp.ok) {
    throw new Error(`backend returned HTTP ${resp.status}`);
  }
  return (await resp.json()) as Health;
}

export async function fetchServices(): Promise<ServiceStatus[]> {
  const resp = await fetch("/api/services");
  if (!resp.ok) throw new Error(`services error: HTTP ${resp.status}`);
  const data = (await resp.json()) as { services: ServiceStatus[] };
  return data.services ?? [];
}

export interface CopilotSettings {
  provider: string;            // "claude" | "ollama"
  model: string;
  providers: string[];         // selectable providers
  base_url?: string;
  has_key?: boolean;
  reachable?: boolean | null;  // null = not an OpenAI-compatible provider / no probe
  models?: string[];           // available cloud models (when reachable)
  error?: string;
}

export async function fetchSettings(): Promise<CopilotSettings> {
  const resp = await fetch("/api/settings");
  if (!resp.ok) throw new Error(`settings error: HTTP ${resp.status}`);
  const data = (await resp.json()) as { copilot: CopilotSettings };
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
      detail = ((await resp.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return ((await resp.json()) as { copilot: CopilotSettings }).copilot;
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
      detail = ((await resp.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return (await resp.json()) as ServiceStatus;
}

export interface ActivityEntry {
  service: string;
  correlation_key: string;
  status: string;
  phase?: string | null;
  updated_at: string;
  title?: string | null;
}

export async function fetchActivity(limit = 50): Promise<ActivityEntry[]> {
  const resp = await fetch(`/api/activity?limit=${limit}`);
  if (!resp.ok) throw new Error(`activity error: HTTP ${resp.status}`);
  const data = (await resp.json()) as { activity: ActivityEntry[] };
  return data.activity ?? [];
}

export async function fetchWorkItems(): Promise<WorkItem[]> {
  const resp = await fetch("/api/workitems");
  if (!resp.ok) {
    throw new Error(`backend returned HTTP ${resp.status}`);
  }
  const body = (await resp.json()) as { count: number; items: WorkItem[] };
  return body.items;
}

export interface LiveProgress {
  correlation_key: string;
  service: string;
  phase: string | null;
  percent: number | null;
  subtask?: string | null;
  updated_at: string;
}

export type FeedMessage =
  | { type: "workitem"; item: WorkItem }
  | { type: "snapshot"; items: WorkItem[] }
  | { type: "progress"; item: LiveProgress };

export async function fetchProgress(): Promise<LiveProgress[]> {
  const resp = await fetch("/api/progress");
  if (!resp.ok) throw new Error(`progress error: HTTP ${resp.status}`);
  return ((await resp.json()) as { items: LiveProgress[] }).items;
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
        onMessage(JSON.parse(ev.data) as FeedMessage);
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

export interface LiveAgent {
  correlation_key: string;
  spec_id: string;
  service: string;
  title: string | null;
  phase: string | null;
  status: string | null;
  // Cockpit-side proxy path the backend re-streams from; never a raw AIFactory URL.
  ws_path: string;
}

export interface LiveAgentsResponse {
  rmux_enabled: boolean;
  count: number;
  agents: LiveAgent[];
}

export async function fetchLiveAgents(): Promise<LiveAgentsResponse> {
  const resp = await fetch("/api/live-agents");
  if (!resp.ok) throw new Error(`live-agents error: HTTP ${resp.status}`);
  return (await resp.json()) as LiveAgentsResponse;
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

export interface ProcessSubtask {
  title: string | null;
  status: string | null;
}

// --- Live execution graph (#94): the animated DAG behind the task detail ---
//
// A stage (plan/code/test) exposes its work as a list of nodes with dependency
// edges; the cockpit lays them out as wave-columns and animates them live. The
// contract is deliberately minimal + additive — a node only needs an id, a
// label and a raw status; everything else (deps for edges, the timestamps for
// per-node timers, kind for the accent colour) is optional and the diagram
// degrades gracefully when a producer can't supply it yet.

export interface FlowNode {
  id: string;
  label: string;
  // Domain tag used only for the accent dot: PFactory child kind
  // (feature/testing/cicd/infra/docs), a TFactory lane name, or a phase.
  kind?: string | null;
  // Raw status string from the producer; classified client-side (see taskFlow.ts)
  // into pending | active | done | failed | stalled.
  status?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  // IDs of prerequisite nodes — each becomes an edge dep → node.
  deps?: string[];
  // Optional producer-computed column; when absent it's derived from deps.
  wave?: number | null;
}

export interface ProcessGraph {
  stage: "plan" | "code" | "test";
  nodes: FlowNode[];
  title?: string | null;
}

export interface ProcessDetail {
  available: boolean;
  correlation_key: string;
  service?: string;
  task_id?: string | null;
  title?: string | null;
  status?: string | null;
  phase?: string | null;
  reason?: string;
  progress?: {
    phase: string | null;
    phase_percent: number | null;
    overall_percent: number | null;
    current_subtask: string | null;
    message: string | null;
  };
  subtasks?: ProcessSubtask[];
  // The live execution DAG for this stage (#94). Best-effort + additive: absent
  // on older builds / producers, in which case the diagram simply doesn't render.
  // `graph` is the furthest stage (default view); `graphs` carries every available
  // stage so the modal can switch between plan / code / test.
  graph?: ProcessGraph | null;
  graphs?: Partial<Record<"plan" | "code" | "test", ProcessGraph>>;
  branch?: string | null;
  updated_at?: string | null;
}

export async function fetchProcess(correlationKey: string): Promise<ProcessDetail> {
  const resp = await fetch(`/api/workitems/${encodeURIComponent(correlationKey)}/process`);
  if (!resp.ok) throw new Error(`process error: HTTP ${resp.status}`);
  return (await resp.json()) as ProcessDetail;
}

export interface Anomaly {
  kind: string;
  severity: string;
  correlation_key: string;
  title: string | null;
  detail: string;
}

export interface Rollups {
  total_work_items: number;
  by_stage: { plan: number; code: number; test: number };
  total_events: number;
  latency: { avg_seconds: number; max_seconds: number } | null;
}

export async function askCopilot(question: string): Promise<string> {
  const resp = await fetch("/api/copilot/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!resp.ok) throw new Error(`copilot error: HTTP ${resp.status}`);
  const body = (await resp.json()) as { answer: string };
  return body.answer;
}

export async function fetchAnomalies(): Promise<Anomaly[]> {
  const resp = await fetch("/api/anomalies");
  if (!resp.ok) throw new Error(`anomalies error: HTTP ${resp.status}`);
  return ((await resp.json()) as { anomalies: Anomaly[] }).anomalies;
}

export async function fetchRollups(): Promise<Rollups> {
  const resp = await fetch("/api/rollups");
  if (!resp.ok) throw new Error(`rollups error: HTTP ${resp.status}`);
  return (await resp.json()) as Rollups;
}

export interface ActionStep {
  method: string;
  endpoint: string;
  payload?: Record<string, unknown>;
}

export interface PreparedAction {
  kind: string;
  correlation_key: string;
  target_service: string;
  method: string;
  endpoint: string;
  payload: Record<string, unknown>;
  follow_ups?: ActionStep[];
  rationale: string;
}

export interface ExecuteResult {
  status_code: number;
  ok: boolean;
  body?: unknown;
  error?: string;
  steps?: { method: string; endpoint: string; status_code: number; ok: boolean }[];
}

export interface AuditEntry {
  id: number;
  ts: string;
  actor: string;
  kind: string;
  correlation_key: string;
  target_service: string;
  endpoint: string;
  status_code: number;
  ok: boolean;
}

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
  return (await resp.json()) as PreparedAction;
}

// Execute a CONFIRMED PreparedAction — the explicit human write step.
export async function executeAction(action: PreparedAction): Promise<ExecuteResult> {
  const resp = await fetch("/api/actions/execute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action),
  });
  if (!resp.ok) throw new Error(`execute failed: HTTP ${resp.status}`);
  return (await resp.json()) as ExecuteResult;
}

export async function fetchAudit(): Promise<AuditEntry[]> {
  const resp = await fetch("/api/audit");
  if (!resp.ok) throw new Error(`audit error: HTTP ${resp.status}`);
  return ((await resp.json()) as { count: number; entries: AuditEntry[] }).entries;
}

export interface ConnectToken {
  // The CFactory API token to use from an editor / external client. Empty in
  // OPEN mode (no CFACTORY_API_KEYS configured).
  token: string;
  configured: boolean;
  // Public base URL of the token-gated API surface (the editor points here).
  // Empty when not configured (CFACTORY_PUBLIC_API_URL unset).
  connect_url: string;
}

// The API token shown on the /settings/token copy page (#73). Guarded behind the
// same gate as the rest of the cockpit UI.
export async function fetchConnectToken(): Promise<ConnectToken> {
  const resp = await fetch("/api/settings/token");
  if (!resp.ok) throw new Error(`token error: HTTP ${resp.status}`);
  return (await resp.json()) as ConnectToken;
}

// Best-effort poll of all upstream services; returns the per-service summary.
export async function refresh(): Promise<Record<string, unknown>> {
  const resp = await fetch("/api/refresh", { method: "POST" });
  if (!resp.ok) {
    throw new Error(`refresh failed: HTTP ${resp.status}`);
  }
  const body = (await resp.json()) as { refreshed: Record<string, unknown> };
  return body.refreshed;
}

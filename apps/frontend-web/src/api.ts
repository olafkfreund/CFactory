// Thin client for the CFactory backend. In dev, Vite proxies /health and /api
// to the backend (see vite.config.ts).

export interface Health {
  status: string;
  service: string;
  version: string;
  upstreams: Record<string, string>;
}

export interface ServiceState {
  task_id: string | null;
  status: string | null;
  phase: string | null;
}

export interface WorkItem {
  correlation_key: string;
  title: string | null;
  pfactory: ServiceState;
  aifactory: ServiceState;
  tfactory: ServiceState;
  timeline: unknown[];
}

export async function fetchHealth(): Promise<Health> {
  const resp = await fetch("/health");
  if (!resp.ok) {
    throw new Error(`backend returned HTTP ${resp.status}`);
  }
  return (await resp.json()) as Health;
}

export async function fetchWorkItems(): Promise<WorkItem[]> {
  const resp = await fetch("/api/workitems");
  if (!resp.ok) {
    throw new Error(`backend returned HTTP ${resp.status}`);
  }
  const body = (await resp.json()) as { count: number; items: WorkItem[] };
  return body.items;
}

export type FeedMessage =
  | { type: "workitem"; item: WorkItem }
  | { type: "snapshot"; items: WorkItem[] };

// Open the live cockpit feed. Returns the socket so the caller can close it.
export function openFeed(onMessage: (msg: FeedMessage) => void, onOpen?: () => void, onClose?: () => void): WebSocket {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/api/ws`);
  ws.onmessage = (ev) => {
    try {
      onMessage(JSON.parse(ev.data) as FeedMessage);
    } catch {
      /* ignore malformed frames */
    }
  };
  if (onOpen) ws.onopen = onOpen;
  if (onClose) ws.onclose = onClose;
  return ws;
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

export interface PreparedAction {
  kind: string;
  correlation_key: string;
  target_service: string;
  method: string;
  endpoint: string;
  payload: Record<string, unknown>;
  rationale: string;
}

export interface ExecuteResult {
  status_code: number;
  ok: boolean;
  body?: unknown;
  error?: string;
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
): Promise<PreparedAction> {
  const resp = await fetch("/api/actions/propose", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, correlation_key }),
  });
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

// Best-effort poll of all upstream services; returns the per-service summary.
export async function refresh(): Promise<Record<string, unknown>> {
  const resp = await fetch("/api/refresh", { method: "POST" });
  if (!resp.ok) {
    throw new Error(`refresh failed: HTTP ${resp.status}`);
  }
  const body = (await resp.json()) as { refreshed: Record<string, unknown> };
  return body.refreshed;
}

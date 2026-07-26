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

// Shared GET helper: fetch a path, throw on non-2xx with a labelled message,
// then validate the body against `schema` (returning the inferred type). The
// thrown message is identical to the previous hand-rolled calls
// (`<label> error: HTTP <status>`), so callers behave exactly as before.
async function getJson<T>(
  path: string,
  // Input is `unknown` (a parsed JSON body), not `T` — so a schema with a
  // `.default()`, whose input and output types differ, is accepted here.
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
  label: string,
): Promise<T> {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`${label} error: HTTP ${resp.status}`);
  return schema.parse(await resp.json());
}

// A non-2xx from the backend, carrying the status alongside the message.
//
// Extends Error, so every existing `e instanceof Error ? e.message : ...` caller
// is unaffected and still shows exactly the same sentence. The status is here for
// the handful of codes that mean something specific to a caller rather than
// "that failed" — a 503 on a credential write is the deployment having no
// encryption key, which the user has to be told plainly (RFC-0020 §3.4).
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// Shared write helper (PUT/POST + JSON body). On a non-2xx it surfaces the
// backend's `{detail}` message when present (falling back to `HTTP <status>`),
// then validates the body against `schema`. Identical behavior to the
// hand-rolled updateCopilotSettings/updateService blocks it replaces.
async function sendJson<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body: unknown,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  const resp = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new ApiError(await errorDetail(resp), resp.status);
  return schema.parse(await resp.json());
}

// Surface the backend's `{detail}` message when present, falling back to
// `HTTP <status>` for a non-JSON error body.
//
// `detail` is normally a string. A refused stage action (RFC-0020 §3.7) instead
// sends `{reason, message}` so a caller can branch on the machine-readable code;
// accept either shape and show the human sentence, which is what a banner needs.
const ErrorDetailSchema = z.object({
  detail: z.union([z.string(), z.object({ reason: z.string(), message: z.string() })]).optional(),
});

async function errorDetail(resp: Response): Promise<string> {
  try {
    const { detail } = ErrorDetailSchema.parse(await resp.json());
    if (typeof detail === "string") return detail;
    if (detail) return detail.message;
    return `HTTP ${resp.status}`;
  } catch {
    return `HTTP ${resp.status}`;
  }
}

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
  // #167 / Factory#272: the routing tier actually picked for this stage, where
  // the pick came from in the precedence chain (pinned/override/policy/default),
  // and the producer-computed saving vs the default tier (metered runs only).
  tier: z.string().nullable().optional(),
  tier_source: z.string().nullable().optional(),
  savings_usd: z.number().nullable().optional(),
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

// Factory#273: prompt-injection scan verdict a service attached when it ran the
// scan. Tolerant by design — the upstream shape is still landing, so only the
// fields the cockpit renders are named and everything else passes through.
export const InjectionScanSchema = z
  .object({
    verdict: z.string().optional(), // "clean" | "flagged"
    reason: z.string().nullable().optional(),
  })
  .passthrough();
export type InjectionScan = z.infer<typeof InjectionScanSchema>;

// TFactory#650: dependency-review signal — pass/fail/warn plus findings.
export const DependencyReviewSchema = z
  .object({
    status: z.string().optional(), // "pass" | "fail" | "warn"
    findings: z
      .array(
        z
          .object({
            package: z.string().optional(),
            severity: z.string().optional(),
            reason: z.string().optional(),
          })
          .passthrough(),
      )
      .optional(),
  })
  .passthrough();
export type DependencyReview = z.infer<typeof DependencyReviewSchema>;

// TFactory#649: the judge-vote split behind a verdict (majority + dissent).
export const VoteSplitSchema = z
  .object({
    verdict: z.string().nullable().optional(),
    majority: z.number().optional(),
    dissent: z.number().optional(),
    votes: z
      .array(
        z
          .object({
            judge: z.string().optional(),
            model: z.string().optional(),
            verdict: z.string().optional(),
          })
          .passthrough(),
      )
      .optional(),
  })
  .passthrough();
export type VoteSplit = z.infer<typeof VoteSplitSchema>;

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
      injection_scan: InjectionScanSchema.optional(),
      dependency_review: DependencyReviewSchema.optional(),
      votes: VoteSplitSchema.optional(),
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
  // #167: producer-computed routing saving (default tier minus actual tier for
  // the tokens actually used). Present only on metered rows that carried one.
  routing_savings_usd: z.number().optional(),
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
    // #167: fleet routing savings — sum of per-task metered savings. Present
    // only when at least one task carried one (metered modes only, no notional
    // dollars for subscription/local runs).
    routing_savings_usd: z.number().optional(),
  }),
  by_service: z.record(ServiceTokensSchema),
  by_work_item: z.array(WorkItemTokenRowSchema),
});
export type TokenTotals = z.infer<typeof TokenTotalsSchema>;

export async function fetchTokens(): Promise<TokenTotals> {
  return getJson("/api/tokens", TokenTotalsSchema, "tokens");
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
  return getJson("/api/tokens/by_worker", TokensByWorkerSchema, "tokens by_worker");
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
  return getJson(
    `/api/tasks/${encodeURIComponent(correlationKey)}/cost-routing`,
    CostRoutingSchema,
    "cost-routing",
  );
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
});
export type WorkerProgress = z.infer<typeof WorkerProgressSchema>;

// Best-effort: the caller swallows failures and keeps the last-good render, so a
// transient error never blanks a running card.
export async function fetchWorkerProgress(correlationKey: string): Promise<WorkerProgress> {
  return getJson(
    `/api/tasks/${encodeURIComponent(correlationKey)}/worker-progress`,
    WorkerProgressSchema,
    "worker-progress",
  );
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
  const data = await getJson(
    "/api/services",
    z.object({ services: z.array(ServiceStatusSchema) }),
    "services",
  );
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
  return getJson("/api/provider-health", ProviderHealthSchema, "provider-health");
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

// `tenant` is the tenant the BACKEND resolved this request to (RFC-0020 §3.3).
// The cockpit cannot know it on its own: it is injected by oauth2-proxy from the
// Keycloak claim and is deliberately not the browser's to choose, so the backend
// says what it resolved to and the Settings panel uses it to address its own git
// configuration.
export async function fetchSettings(): Promise<CopilotSettings & { tenant: string }> {
  const data = await getJson(
    "/api/settings",
    z.object({ copilot: CopilotSettingsSchema, tenant: z.string().default("default") }),
    "settings",
  );
  return { ...data.copilot, tenant: data.tenant };
}

// ── Tenant git configuration (RFC-0020 §3.3) ────────────────────────────────

// The MASKED credential indicator (RFC-0020 §3.4). Whether this tenant has a
// credential, when it was stored, and which key wraps it — and deliberately no
// field for the credential itself, not even a masked prefix. The cockpit cannot
// render what it is never sent, which is the point: a value that never reaches
// the browser cannot leak from it.
export const CredentialInfoSchema = z.object({
  configured: z.boolean(),
  source: z.string(), // "tenant" | "env" | "none"
  updated_at: z.string().nullable().optional(),
  key_version: z.string().nullable().optional(),
});
export type CredentialInfo = z.infer<typeof CredentialInfoSchema>;

// A tenant has many CONNECTIONS (a provider, a host, a credential) and each
// connection has many REPOSITORIES (a project path, where issues are imported
// from, which AIFactory project its builds land in). Exactly one repository is
// the tenant DEFAULT — what a card that names no repository resolves to.
//
// `GET .../git-connections` returns the whole tree in one call, so every mutation
// below is followed by a re-read rather than a local patch: `status` is derived
// on the backend and deleting a repository can promote a new default, so guessing
// at either here would make the panel a second, wrong source of truth.

export const GitRepositorySchema = z.object({
  id: z.number(),
  connection_id: z.number(),
  tenant_id: z.string(),
  project: z.string(),
  intake_project: z.string().nullable().optional(),
  // An AIFactory PROJECT ID (a UUID), not a repository path — the two are
  // separate fields precisely because confusing them is easy.
  aifactory_project_id: z.string().nullable().optional(),
  default_labels: z.array(z.string()).default([]),
  is_default: z.boolean().default(false),
  created_at: z.string().nullable().optional(),
  updated_at: z.string().nullable().optional(),
});
export type GitRepository = z.infer<typeof GitRepositorySchema>;

export const GitConnectionSchema = z.object({
  id: z.number(),
  tenant_id: z.string(),
  provider: z.string(), // "github" | "gitlab" | "azure_devops"
  // Always the RESOLVED host (the tenant's own, or the provider default).
  base_url: z.string(),
  label: z.string(),
  // unconfigured | credential_missing | configured | verified — derived, never stored.
  status: z.string(),
  // Defaulted rather than required so a cockpit that reaches an older backend
  // renders "no credential" instead of failing to parse the whole panel.
  credential: CredentialInfoSchema.default({ configured: false, source: "none" }),
  verified_at: z.string().nullable().optional(),
  verify_error: z.string().nullable().optional(),
  repositories: z.array(GitRepositorySchema).default([]),
});
export type GitConnection = z.infer<typeof GitConnectionSchema>;

export const GitConnectionsSchema = z.object({
  connections: z.array(GitConnectionSchema).default([]),
  default_repository_id: z.number().nullable().optional(),
});
export type GitConnections = z.infer<typeof GitConnectionsSchema>;

// A verify never fails the request: an unreachable host is `ok: false` with the
// reason. `connection` is the connection as it stands after the attempt.
export const GitVerifySchema = z.object({
  ok: z.boolean(),
  reason: z.string().nullable().optional(),
  repository: z.string().nullable().optional(),
  verified_project: z.string().nullable().optional(),
  status: z.string().optional(),
  connection: GitConnectionSchema.optional(),
});
export type GitVerify = z.infer<typeof GitVerifySchema>;

// The shape every delete answers with. Deliberately loose about the rest: the
// panel re-reads the tree afterwards, so only "it worked" is consumed here.
export const GitDeleteSchema = z
  .object({ ok: z.boolean(), deleted: z.boolean().optional() })
  .passthrough();

export type GitRepositoryBody = {
  project: string;
  intake_project?: string | null;
  aifactory_project_id?: string | null;
  default_labels?: string[];
};

function tenantPath(tenant: string, rest: string): string {
  return `/api/tenants/${encodeURIComponent(tenant)}/${rest}`;
}

export async function fetchGitConnections(tenant: string): Promise<GitConnections> {
  return getJson(tenantPath(tenant, "git-connections"), GitConnectionsSchema, "git-connections");
}

export async function createGitConnection(
  tenant: string,
  body: { provider: string; base_url?: string | null; label?: string | null },
): Promise<GitConnection> {
  return sendJson("POST", tenantPath(tenant, "git-connections"), body, GitConnectionSchema);
}

// A patch: only the fields sent are applied. Moving a connection's provider or
// host clears its verification, which proved a connection it no longer is.
export async function updateGitConnection(
  tenant: string,
  id: number,
  body: { provider?: string; base_url?: string | null; label?: string | null },
): Promise<GitConnection> {
  return sendJson(
    "PATCH",
    tenantPath(tenant, `git-connections/${String(id)}`),
    body,
    GitConnectionSchema,
  );
}

// Takes the connection's repositories and its credential with it.
export async function deleteGitConnection(tenant: string, id: number): Promise<unknown> {
  return sendJson(
    "DELETE",
    tenantPath(tenant, `git-connections/${String(id)}`),
    undefined,
    GitDeleteSchema,
  );
}

export async function verifyGitConnection(tenant: string, id: number): Promise<GitVerify> {
  return sendJson(
    "POST",
    tenantPath(tenant, `git-connections/${String(id)}:verify`),
    {},
    GitVerifySchema,
  );
}

export async function createGitRepository(
  tenant: string,
  connectionId: number,
  body: GitRepositoryBody & { make_default?: boolean },
): Promise<GitRepository> {
  return sendJson(
    "POST",
    tenantPath(tenant, `git-connections/${String(connectionId)}/repositories`),
    body,
    GitRepositorySchema,
  );
}

// A patch, and `null` on intake_project / aifactory_project_id CLEARS it.
export async function updateGitRepository(
  tenant: string,
  id: number,
  body: Partial<GitRepositoryBody>,
): Promise<GitRepository> {
  return sendJson(
    "PATCH",
    tenantPath(tenant, `git-repositories/${String(id)}`),
    body,
    GitRepositorySchema,
  );
}

export async function deleteGitRepository(tenant: string, id: number): Promise<unknown> {
  return sendJson(
    "DELETE",
    tenantPath(tenant, `git-repositories/${String(id)}`),
    undefined,
    GitDeleteSchema,
  );
}

export async function setDefaultGitRepository(tenant: string, id: number): Promise<GitRepository> {
  return sendJson(
    "POST",
    tenantPath(tenant, `git-repositories/${String(id)}:default`),
    {},
    GitRepositorySchema,
  );
}

// ── Connection credential (RFC-0020 §3.4) ───────────────────────────────────
// Write-only. There is no fetch here and no endpoint to write one against: the
// credential goes up, and only the masked indicator comes back. A credential
// belongs to a CONNECTION, not to a repository — it authenticates to a host, and
// two repositories on one host are reached with the same token.

export const GitCredentialResultSchema = z.object({
  ok: z.boolean(),
  removed: z.boolean().optional(),
  credential: CredentialInfoSchema,
});
export type GitCredentialResult = z.infer<typeof GitCredentialResultSchema>;

function credentialPath(tenant: string, connectionId: number): string {
  return tenantPath(tenant, `git-connections/${String(connectionId)}/credential`);
}

export async function setConnectionCredential(
  tenant: string,
  connectionId: number,
  credential: string,
): Promise<GitCredentialResult> {
  return sendJson(
    "PUT",
    credentialPath(tenant, connectionId),
    { credential },
    GitCredentialResultSchema,
  );
}

export async function deleteConnectionCredential(
  tenant: string,
  connectionId: number,
): Promise<GitCredentialResult> {
  return sendJson(
    "DELETE",
    credentialPath(tenant, connectionId),
    undefined,
    GitCredentialResultSchema,
  );
}

export async function updateCopilotSettings(
  provider: string,
  model: string,
): Promise<CopilotSettings> {
  const data = await sendJson(
    "PUT",
    "/api/settings/copilot",
    { provider, model },
    z.object({ copilot: CopilotSettingsSchema }),
  );
  return data.copilot;
}

export async function updateService(name: string, url: string): Promise<ServiceStatus> {
  return sendJson("PUT", `/api/services/${encodeURIComponent(name)}`, { url }, ServiceStatusSchema);
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
  const data = await getJson(
    `/api/activity?limit=${limit}`,
    z.object({ activity: z.array(ActivityEntrySchema) }),
    "activity",
  );
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
  const data = await getJson(
    "/api/progress",
    z.object({ items: z.array(LiveProgressSchema) }),
    "progress",
  );
  return data.items;
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
  // A streamable agent has a live rmux console; a non-streamable row (#184, e.g.
  // a TFactory verify session) is shown as informational — no console to open.
  // Always emitted by the backend (pydantic defaults serialize), so required here.
  streamable: z.boolean(),
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
  return getJson("/api/live-agents", LiveAgentsResponseSchema, "live-agents");
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

// RFC-0015 §4 D2: one requirement->test traceability row — an acceptance
// criterion mapped to its covering test(s), the VAL level reached, and the
// verdict. `tests` empty => the AC has no covering test (a traceability gap).
// `.passthrough()` keeps the row forward-compatible (the contract marks the item
// `additionalProperties: true`).
export const TraceabilityRowSchema = z
  .object({
    ac_id: z.string(),
    ac_text: z.string().nullable().optional(),
    tests: z.array(z.string()).optional(),
    val_level: z.string().nullable().optional(),
    status: z.enum(["passed", "failed", "not_run", "skipped"]),
  })
  .passthrough();
export type TraceabilityRow = z.infer<typeof TraceabilityRowSchema>;

// RFC-0015 §3.3: the human-readable spec/plan/tasks Markdown mirror PFactory
// emits from the contract — rendered in the cockpit so reviewers read the plan as
// docs, not stringified structures. Each field optional; absent => not rendered.
export const ArtifactsSchema = z
  .object({
    spec: z.string().optional(),
    plan: z.string().optional(),
    tasks: z.string().optional(),
  })
  .passthrough();
export type Artifacts = z.infer<typeof ArtifactsSchema>;

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
  // RFC-0015 §3.3: readable spec/plan/tasks Markdown. Absent on tasks whose
  // producer didn't emit the mirror — the artifacts panel then doesn't render.
  artifacts: ArtifactsSchema.nullable().optional(),
  // RFC-0015 §4 D2: requirement->test traceability rows (AC x test x VAL x
  // verdict). Absent => the matrix shows a "not available" note, never an empty
  // table or a crash.
  traceability: z.array(TraceabilityRowSchema).nullable().optional(),
});
export type ProcessDetail = z.infer<typeof ProcessDetailSchema>;

export async function fetchProcess(correlationKey: string): Promise<ProcessDetail> {
  return getJson(
    `/api/workitems/${encodeURIComponent(correlationKey)}/process`,
    ProcessDetailSchema,
    "process",
  );
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
  const data = await getJson(
    "/api/anomalies",
    z.object({ anomalies: z.array(AnomalySchema) }),
    "anomalies",
  );
  return data.anomalies;
}

export async function fetchRollups(): Promise<Rollups> {
  return getJson("/api/rollups", RollupsSchema, "rollups");
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
  const data = await getJson(
    "/api/audit",
    z.object({ count: z.number(), entries: z.array(AuditEntrySchema) }),
    "audit",
  );
  return data.entries;
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
  return getJson("/api/settings/token", ConnectTokenSchema, "token");
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

// --- Federated search (#149) ----------------------------------------------

// One ranked hit from the cockpit's cross-portal search. The cockpit already
// aggregates every producer (Plan/Build/Test) into one store, so a single query
// spans all four portals; `services` names which portals carry state and
// `matched_on` explains why it ranked (key/title/repo/state).
export const SearchResultSchema = z.object({
  correlation_key: z.string(),
  title: z.string().nullable(),
  services: z.array(z.string()),
  repo: z.string().nullable(),
  status: z.string().nullable(),
  score: z.number(),
  matched_on: z.array(z.string()),
  updated_at: z.string().nullable(),
});
export type SearchResult = z.infer<typeof SearchResultSchema>;

const SearchResponseSchema = z.object({
  query: z.string(),
  count: z.number(),
  results: z.array(SearchResultSchema),
});

// Federated search across all portals' work. Blank query returns nothing.
export async function searchWorkItems(q: string, limit = 20): Promise<SearchResult[]> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  const body = await getJson(`/api/search?${params.toString()}`, SearchResponseSchema, "search");
  return body.results;
}

// --- Planning cards (RFC-0019 Phase 1, #302) -------------------------------

// A card is the planning-time home of a correlation: humans (and, via MCP,
// agents) create and prioritise cards; when one enters the factory it gains a
// `correlation_key` and threads the existing work-item timeline. Statuses are
// the PLANNING axis (backlog → done) and are orthogonal to the PARR pipeline
// stage statuses rendered by Board.tsx.
export const CardStatusSchema = z.enum(["backlog", "ready", "in_progress", "blocked", "done"]);
export type CardStatus = z.infer<typeof CardStatusSchema>;

// Difficulty tiers reuse RFC-0011's intake vocabulary.
export const CardTierSchema = z.enum(["low", "medium", "hard"]);
export type CardTier = z.infer<typeof CardTierSchema>;

// The three pipeline stages a card can be pushed into (RFC-0020 §3.7).
export const CardStageSchema = z.enum(["plan", "code", "test"]);
export type CardStage = z.infer<typeof CardStageSchema>;

// One stage's dispatch record: what the factory was actually asked to do, and
// how it turned out. `queued` is committed to by a sequence but not yet sent.
export const StageRunSchema = z
  .object({
    service: z.string().optional(),
    status: z.enum(["queued", "dispatched", "done", "failed"]),
    dispatched_at: z.string().nullable().optional(),
    ref: z.string().nullable().optional(),
    detail: z.string().nullable().optional(),
  })
  .passthrough();
export type StageRun = z.infer<typeof StageRunSchema>;

export const CardSchema = z
  .object({
    card_key: z.string(),
    tenant_id: z.string(),
    title: z.string(),
    // Free-form markdown body; where an imported issue's body lands (RFC-0020 §3.6).
    description: z.string().nullish(),
    acceptance_criteria: z.array(z.string()),
    status: CardStatusSchema,
    // Lower is higher priority (1 sorts above 2).
    priority: z.number(),
    tier: CardTierSchema.nullable(),
    assignee: z.string().nullable(),
    milestone: z.string().nullable(),
    // Set once the card's work enters the factory (RFC-0001 correlation), and
    // then REUSED by every later stage rather than re-pointed.
    correlation_key: z.string().nullable(),
    // Mirrored FROM the git host and never pushed to it (RFC-0019 §3.5): which
    // issue this card is the projection of ("owner/repo#123"), that issue's state
    // there, and its labels. The ref carries no HOST — the board is
    // multi-provider, so a link out is built from the connection THIS CARD's
    // repository is reached through, never from a hardcoded github.com (see
    // `issueUrlResolver` in cards.ts). Nullish/defaulted so a card served by a
    // pre-Phase-6 backend still parses.
    issue_ref: z.string().nullish(),
    // Which of the tenant's repositories this card targets (RFC-0020 §3.3).
    // Null/absent means the tenant DEFAULT repository, which is what every card
    // created before there was more than one means.
    repository_id: z.number().nullish(),
    issue_state: z.string().nullish(),
    labels: z.array(z.string()).default([]),
    // Per-stage dispatch records, keyed by stage name. Optional so a card served
    // by a pre-Phase-7 backend still parses.
    stage_runs: z.record(CardStageSchema, StageRunSchema).default({}),
    // The imported issue DISCUSSION, as two scalars (Factory#375). The bodies are
    // a separate GET — 46 cards each carrying a full thread is a payload nobody
    // asked for, and the thread renders collapsed anyway. `comments_synced_at`
    // null means the thread has never been read successfully, which is NOT the
    // same as `comment_count === 0` beside a timestamp (an issue with no
    // discussion). Defaulted/nullish so a pre-#375 backend still parses.
    comment_count: z.number().default(0),
    comments_synced_at: z.string().nullish(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .passthrough();
export type Card = z.infer<typeof CardSchema>;

// The list endpoint's envelope: tolerate both a bare array and the `{cards}`
// envelope the sibling endpoints use, so either backend shape round-trips.
const CardListSchema = z.union([
  z.array(CardSchema),
  z.object({ cards: z.array(CardSchema) }).passthrough(),
]);

export type CardFilters = Partial<Record<"status" | "milestone" | "assignee" | "tier", string>>;

// Fields a human (or an agent) may change on an existing card. `status` +
// `priority` are the move/reprioritise pair the planning board drives.
export type CardPatch = Partial<
  Pick<
    Card,
    "title" | "acceptance_criteria" | "status" | "priority" | "tier" | "assignee" | "milestone"
  >
>;

export type CardCreate = Pick<Card, "title"> &
  Partial<
    Pick<Card, "acceptance_criteria" | "status" | "priority" | "tier" | "assignee" | "milestone">
  >;

export async function fetchCards(filters: CardFilters = {}): Promise<Card[]> {
  const params = new URLSearchParams(
    Object.entries(filters).filter((e): e is [string, string] => Boolean(e[1])),
  );
  const qs = params.toString();
  const body = await getJson(`/api/cards${qs ? `?${qs}` : ""}`, CardListSchema, "cards");
  return Array.isArray(body) ? body : body.cards;
}

export async function fetchCard(cardKey: string): Promise<Card> {
  return getJson(`/api/cards/${encodeURIComponent(cardKey)}`, CardSchema, "card");
}

export async function createCard(card: CardCreate): Promise<Card> {
  return sendJson("POST", "/api/cards", card, CardSchema);
}

export async function patchCard(cardKey: string, patch: CardPatch): Promise<Card> {
  return sendJson("PATCH", `/api/cards/${encodeURIComponent(cardKey)}`, patch, CardSchema);
}

// One imported issue comment (Factory#375). `body` is third-party markdown and
// is rendered through the shared `Markdown` component, which emits React nodes
// and never HTML — an issue comment is exactly the hostile input that must not
// be able to inject markup.
export const CardCommentSchema = z
  .object({
    comment_id: z.string(),
    author: z.string(),
    body: z.string(),
    url: z.string(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .passthrough();
export type CardComment = z.infer<typeof CardCommentSchema>;

// `synced_at` null means the thread has never been read successfully — never
// imported, or the last read failed. An empty `comments` list BESIDE a timestamp
// means the issue genuinely has no discussion. The cockpit must not conflate the
// two, so the schema keeps them apart.
export const CardCommentsSchema = z
  .object({
    card_key: z.string(),
    issue_ref: z.string().nullish(),
    count: z.number(),
    synced_at: z.string().nullish(),
    comments: z.array(CardCommentSchema),
  })
  .passthrough();
export type CardComments = z.infer<typeof CardCommentsSchema>;

export async function fetchCardComments(cardKey: string): Promise<CardComments> {
  return getJson(
    `/api/cards/${encodeURIComponent(cardKey)}/comments`,
    CardCommentsSchema,
    "card comments",
  );
}

// Importing a repository's EXISTING issues (RFC-0020 §3.6). Poll-based, NOT
// live: `last_synced_at` says how fresh the board is, and `truncated` says the
// run hit CFACTORY_IMPORT_MAX so the board is NOT the whole backlog.
export const CardImportSchema = z
  .object({
    ok: z.boolean(),
    project: z.string(),
    imported: z.number(),
    updated: z.number(),
    skipped: z.number(),
    truncated: z.boolean(),
    // The incremental cursor. `polled_at` beside it is when the read happened,
    // which is what "last synced" means to a human (#374).
    last_synced_at: z.string().nullish(),
    polled_at: z.string().nullish(),
    reason: z.string().nullish(),
  })
  .passthrough();
export type CardImport = z.infer<typeof CardImportSchema>;

export async function importCards(): Promise<CardImport> {
  return sendJson("POST", "/api/cards/import", {}, CardImportSchema);
}

// How CURRENT the board is, per connected repository (#374). `last_polled_at` is
// when that repository's issues were last read successfully — which is NOT
// `watermark_at`, the incremental cursor, because the cursor does not move on a
// repository nobody has touched. `stale` is the server's rule (no successful read
// for more than two poll cadences), computed there so every client agrees.
export const CardSyncRepoSchema = z.object({
  repository_id: z.number().nullable(),
  project: z.string(),
  is_default: z.boolean(),
  last_polled_at: z.string().nullable(),
  watermark_at: z.string().nullable(),
  stale: z.boolean(),
});
export type CardSyncRepo = z.infer<typeof CardSyncRepoSchema>;

export const CardSyncStateSchema = z.object({
  now: z.string(),
  poll: z.object({
    enabled: z.boolean(),
    interval_seconds: z.number(),
    live: z.boolean(),
  }),
  repositories: z.array(CardSyncRepoSchema),
});
export type CardSyncState = z.infer<typeof CardSyncStateSchema>;

export async function fetchCardSyncState(): Promise<CardSyncState> {
  return getJson("/api/cards/sync-state", CardSyncStateSchema, "card sync-state");
}

// What a stage action answers with: what the dispatch did, and the card as it now
// stands. Deliberately tolerant — the dispatch half mirrors an upstream response
// whose shape is the factory's, not ours.
export const StageActionResultSchema = z
  .object({
    stage: z
      .object({
        stage: CardStageSchema.optional(),
        dispatched: z.boolean().optional(),
        ok: z.boolean().optional(),
        reason: z.string().optional(),
        skipped: z.string().optional(),
        target_service: z.string().optional(),
        sequence: z.array(CardStageSchema).optional(),
        warnings: z.array(z.string()).optional(),
      })
      .passthrough(),
    card: CardSchema.nullable(),
  })
  .passthrough();
export type StageActionResult = z.infer<typeof StageActionResultSchema>;

/**
 * Push a card into one stage, or through the whole sequence when `action` is
 * "run" (RFC-0020 §3.7). A REFUSED transition is a 409 whose `detail.message`
 * `sendJson` throws verbatim, so the caller surfaces the backend's own sentence
 * rather than inventing one.
 */
export async function runCardStage(
  cardKey: string,
  action: CardStage | "run",
): Promise<StageActionResult> {
  return sendJson(
    "POST",
    `/api/cards/${encodeURIComponent(cardKey)}/actions/${action}`,
    {},
    StageActionResultSchema,
  );
}

export async function deleteCard(cardKey: string): Promise<void> {
  const resp = await fetch(`/api/cards/${encodeURIComponent(cardKey)}`, { method: "DELETE" });
  // 204 (no body) is the expected success shape; anything non-2xx surfaces the
  // backend detail exactly like the JSON writers above.
  if (!resp.ok) throw new Error(await errorDetail(resp));
}

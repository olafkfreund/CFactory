---
layout: default
title: Architecture
permalink: /architecture/
---

# Architecture

CFactory is a **read-first, advise-and-confirm cockpit** layered over the three
Factory services. It owns no pipeline logic of its own — it observes, correlates
and (with a human click) triggers the services through their existing APIs.

## The big picture

```
        ┌───────────┐     ┌───────────┐     ┌───────────┐
        │ PFactory  │     │ AIFactory │     │ TFactory  │
        │  :3105    │     │  :3101    │     │  :3103    │
        │  (Plan)   │ ──▶ │  (Act)    │ ──▶ │ (Verify)  │
        └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
              │  REST · WebSocket · completion webhooks
              ▼                 ▼                 ▼
        ┌──────────────────────────────────────────────┐
        │                  CFactory                     │
        │  ┌────────────┐   ┌──────────────┐            │
        │  │  Adapters  │──▶│  WorkItem    │            │
        │  │ (per svc)  │   │  store (PG)  │            │
        │  └────────────┘   └──────┬───────┘            │
        │  ┌────────────┐          │                    │
        │  │  Webhook   │──────────┘                    │
        │  │  ingress   │   ┌──────────────┐            │
        │  └────────────┘   │  Agentic     │            │
        │                   │  copilot     │            │
        │  ┌────────────┐   │ (Claude SDK) │            │
        │  │  Cockpit   │◀──┴──────────────┘            │
        │  │  UI :3110  │   advise + confirm            │
        │  └────────────┘                               │
        │            backend API :3111                  │
        └──────────────────────────────────────────────┘
```

## The data plane

CFactory deliberately uses each service's **existing** surface rather than its
stdio MCP server (which is spawned per-process by an LLM client and unsuited to a
persistent dashboard):

- **REST (pull / state)** — wraps each service's API (PFactory `plan_*`,
  AIFactory `task_*`, TFactory `task_*` / `report_get`) to hydrate state on demand.
- **WebSocket (live)** — subscribes to each service's feed for in-flight phase and
  progress updates.
- **Webhooks (terminal)** — a `POST /api/events` ingress receives a *normalized
  completion envelope* (`{correlation_key, service, task_id, status, phase,
  updated_at}`) from all three services and upserts the WorkItem timeline.

## The linchpin: the WorkItem

The one thing the family lacks today is a shared identity for a unit of work.
CFactory introduces it. A `WorkItem` is keyed by the **GitHub issue number**
(synthetic fallback otherwise) and threads the chain:

```
pfactory.session_id → github issue # → aifactory.task_id → branch / PR # → tfactory.spec_id
```

This is what lets the cockpit — and the copilot — answer "where is feature X"
with history, not just a live snapshot.

## The agentic copilot

An LLM layer (Claude Agent SDK) whose tools are CFactory's *own* functions:

- **Read tools** — query WorkItems, summarise a timeline, compute cost/latency
  rollups, detect anomalies.
- **Action tools (advise + confirm)** — `propose_approve_gate`,
  `propose_trigger_handoff`, `propose_kick_handback`. Each returns a *prepared
  action* (target service, endpoint, payload, rationale) that only executes on an
  explicit human click. **No autonomous writes.**

## Live agent terminals

When AIFactory is executing, the cockpit can stream each agent's terminal into
Mission Control. AIFactory exposes a per-task **rmux** console (a server-side
terminal multiplexer); CFactory's backend lists the active agents
(`GET /api/live-agents`), opens each console WebSocket **server-side**, and
re-streams the raw ANSI bytes to an xterm.js tile in the browser
(`WS /api/live-agents/{key}/ws`).

The proxy is **read-only and single-origin by design**: the cockpit never
attaches or forwards keystrokes, and the AIFactory URL and token never leave the
backend — the browser only ever talks to CFactory. It degrades cleanly when
rmux is disabled or no agents are running.

## Live execution diagram

The task-detail drawer renders the work item as a **live dependency-graph (DAG)**
— an animated picture of the stage that is furthest along. The backend resolves a
single **stage preference of test → code → plan** and emits a shared `graph` field
(stage + nodes + dependency edges) on `GET /api/workitems/{key}/process`, so a
testing item shows its lane pipeline, a coding item its subtask DAG, and a
planning item its plan DAG:

- **code** — AIFactory subtasks become nodes, `depends_on` becomes the edges, and
  each subtask's `started_at` / `completed_at` drives its per-node timer.
- **plan** — a PFactory session's decomposed `epic.children` become nodes (one per
  child, child `kind` as the accent); a plan is a static artifact, so children
  render as the dependency-ordered shape of the work with no live timing.
- **test** — TFactory's lane-tagged subtasks aggregate into one node per lane,
  ordered along the `unit → browser → api → integration → mutation` spine, each
  lane's status rolled up worst-first from its subtasks.

The frontend lays the nodes out as wave-columns (a node's column is one past its
deepest dependency) and animates them live: a node is **done (green + a robot
stamp)**, **active (cyan pulse)**, **failed (red shake)** or **stalled (amber
pulse)**, classified client-side from the raw producer status. The edge the work
is currently flowing along — source done, target not yet done — animates with
marching dashes, and every node carries a live mm:ss timer. It is hand-rolled SVG
plus framer-motion (no graph library) in the gruvbox stage palette.

The whole feature is **additive**: the `graph` field is best-effort, and the
diagram renders nothing when a producer supplies no nodes — older builds and
work items with no decomposition look exactly as they did before.

## Token & cost

Every service attaches the RFC-0001 `usage` block (input/output tokens,
cost, model) to its completion event. CFactory aggregates them into the
**Tokens & cost** page — totals and a per-service, per-work-item breakdown — so
real LLM spend across plan, code and test is visible in one place.

### Usage by task (by billing mode)

The same per-work-item usage surfaces in **Mission Control** as a "Usage by task"
panel. It shows the right metric per **billing mode**, because cost is only real
for metered work: a Claude/Codex/Antigravity *subscription* still makes the SDK
report a notional `cost_usd`, and a local Ollama model costs no dollars at all.
AIFactory classifies each provider's billing mode (`api` / `cloud` /
`subscription` / `local`) and carries it on the `usage.by_provider` rollup; CFactory
buckets each task's usage by mode and shows:

- **api / cloud** (metered) — real dollars, plus budget spend/limit and an
  "over budget" badge when the work item's budget is set;
- **subscription** — tokens + time spent, a "subscription" tag, no dollar figure;
- **local** (Ollama) — tokens + compute time, a "local" tag, no dollar figure;
- **time spent** for every task (wall clock from the event timeline).

The Mission Control headline shows real **Spend (USD)** only when something was
actually metered; otherwise it shows **Tokens**. A row without a billing breakdown
(older / in-flight events) falls back conservatively to tokens + time — never a
notional dollar figure. The panel is fed from CFactory's **own `/api/tokens` event
store** — **not** the metrics backend, which carries only low-cardinality fleet
aggregates with no `task_id`. Running task cards carry the same billing-aware live
**stamp** (see below).

### Per-worker drill-down

A task is usually a fan-out of workers, each on its own slice and often on a
different provider/model. The `WorkItem` carries a `WorkerUsage` list plus
`by_provider` and `by_model` rollups, served at `GET /api/tokens/by_worker`, so
the page can drill from a per-service total down to "which worker, on which model,
cost what". Worker sub-events upsert into a `workers` map keyed by `worker_id`
(**idempotent**); completion events with no worker data ingest exactly as before.
A soft, informational **"over budget" badge** renders only when a work item's
`usage.budget.exceeded` flag is set — surfaced, never enforced.

### Live per-task cost stamp and sparkline

While a task is running, each card shows a **live stamp** — accumulated cost,
tokens, workers-done, elapsed — plus a hand-rolled SVG **sparkline of cumulative
cost** that steps up as each worker finishes. It rides the existing WebSocket
broadcast and poll (no new transport) and is fully additive: cards with no worker
data render unchanged. Throttled `phase:"worker_progress"` heartbeats are ingested
into a rolling per-worker series — **capped at 120 points/worker and pruned on a
terminal event** so the store cannot bloat — exposed at
`GET /api/tasks/{key}/worker-progress` and fed as a **dense** cumulative series
into the sparkline for a smooth ~10s tick (falling back to the stepwise per-worker
series when no heartbeats arrive).

### Per-task detail vs. fleet metrics (a deliberate split)

The per-running-task detail comes from CFactory's **own event store** (the worker
events it already ingests), **not** from the metrics backend. The OpenTelemetry
metrics the services emit are intentionally **low-cardinality** (no `task_id`),
which is what keeps a metrics TSDB healthy but makes it unable to answer "show me
this one task". So the responsibilities split cleanly: **OpenObserve** — a bundled
sibling app the cockpit links to — serves fleet-wide aggregates over those
low-cardinality metrics, while **CFactory** serves the per-task drill-down from its
event store. CFactory deliberately does **not** implement an OTLP receiver or a
time-series database of its own (that would reinvent Grafana/Tempo); the per-task
series is a bounded, capped, pruned slice of the store that already backs the
cockpit.

## What the cockpit shows

The UI is organised as seven views over the same correlated state:

- **Mission Control** — the whole factory at a glance: PARR pipeline counts,
  anomalies, a live agent roster, and a "Cost & tokens by task" panel ranking
  work items by spend.
- **Pipeline** — every work item as a card, threaded across plan → code → test by
  its issue number; click for a detail drawer with the **live execution diagram**
  (the animated stage DAG), live process output and the agent's **rmux** terminal.
- **Running tasks** — live progress across every sibling, filterable by
  `All / Running / Failed / Done`, with per-task phase and progress.
- **Tokens & cost** — real LLM spend, totalled per service and per work item.
- **Copilot** — the agentic chat plus proactive insight cards.
- **Audit** — the live completion-activity feed and the confirmed-actions log.
- **Services** — per-service health with **editable upstream endpoints**, so the
  cockpit can be repointed at a different PFactory/AIFactory/TFactory without a
  redeploy.

## Deployment

CFactory ships as two container images — the **backend** and the **cockpit** —
packaged as a **two-pod Helm chart** (with a `devenv` workflow for local
iteration). Continuous deployment is GitOps-driven: on every push to `main`, CI
builds and pushes sha-tagged images to **GHCR**, then bumps the image tags in the
`factory-gitops` repo so **ArgoCD** reconciles and redeploys the k3d cluster — no
manual rollout step.

The deploy is **gated on a green test run**: a `test` workflow runs the backend
pytest suite (the **Backend pytest** check) plus a frontend TypeScript typecheck
and production build on every PR and push, and the deploy workflow depends on it —
nothing reaches the cluster unless the tests pass first. The backend also refuses
to boot with the default audit-HMAC secret outside local mode, so the
tamper-evident audit chain cannot be silently defeated in production.

## Tech stack

Built on the same skeleton as the rest of the family, so security and operations
match:

- **Backend** — Python 3.13 + FastAPI, Claude Agent SDK
- **Cockpit UI** — React 19 + Vite (port 3110)
- **API** — FastAPI REST + WebSocket (port 3111)
- **Store** — PostgreSQL (reusing AIFactory's data layer)
- **Auth/security** — reuses AIFactory's enterprise modules (scoped keys, SAML/SCIM,
  tenant isolation, HMAC-anchored audit log)
- **Dev env** — Nix flake + direnv, plus a `devenv` workflow
- **Packaging** — two container images (backend + cockpit), a two-pod Helm chart
- **CD** — GitHub Actions → GHCR → `factory-gitops` → ArgoCD on k3d

See the [roadmap](/roadmap/) for how this gets built, phase by phase.

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
        │  :3102    │     │  :3101    │     │  :3103    │
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

## Tech stack

Built on the same skeleton as the rest of the family, so security and operations
match:

- **Backend** — Python 3.13 + FastAPI, Claude Agent SDK
- **Cockpit UI** — React 19 + Vite (port 3110)
- **API** — FastAPI REST + WebSocket (port 3111)
- **Store** — PostgreSQL (reusing AIFactory's data layer)
- **Auth/security** — reuses AIFactory's enterprise modules (scoped keys, SAML/SCIM,
  tenant isolation, HMAC-anchored audit log)
- **Dev env** — Nix flake + direnv

See the [roadmap](/roadmap/) for how this gets built, phase by phase.

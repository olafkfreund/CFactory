---
layout: default
title: Roadmap
permalink: /roadmap/
---

# Roadmap

CFactory is built in phases, tracked as epics in the
[CFactory repo]({{ site.repo_url }}/issues) and rolled up in the
[Factory program board](https://github.com/users/olafkfreund/projects/1).

Sequencing principle: **spine first**. CFactory is only as good as the pipeline it
observes, so the cross-repo PARR spine is standardized before the cockpit goes
deep.

## Phase 0 — Standardize the spine *(prerequisite, tracked in the Factory repo)*

- Wire PFactory → AIFactory inbound handoff with provenance
- Adopt a shared correlation key (the GitHub issue number) end-to-end
- Normalize the completion-event envelope across all three services
- Resolve the PFactory/TFactory port collision (canonical map: AIFactory 3101,
  PFactory 3102, TFactory 3103, CFactory 3110/3111)

## Phase 1 — Skeleton, correlation store & read-only cockpit

- Fork the shared Factory skeleton; ports 3110/3111; `~/.cfactory` workspace ✅
- `WorkItem` correlation model + PostgreSQL persistence
- Service adapters: PFactory, AIFactory, TFactory REST clients
- WebSocket live subscriptions
- Webhook ingress (`POST /api/events`)
- Read-only pipeline board UI

## Phase 2 — Agentic pipeline copilot

- Claude Agent SDK copilot service
- Read tools: query WorkItems, summarise timelines, cost/latency rollups
- Anomaly detection: stuck phases, handback loops, gate failures, cost spikes
- Copilot chat panel + proactive insight cards

## Phase 3 — Advise + confirm actions, audit & scoped keys

- Prepared-action model (`propose_*`)
- Action tools: approve gate, trigger handoff, kick handback
- Human-in-the-loop confirm UI + full audit log
- Scoped API keys with read/write enforcement

## Phase 4 — Harden for hosted / multi-tenant

- Reuse AIFactory enterprise auth (SAML/SCIM, tenant isolation, HMAC audit)
- Docker image + Helm chart
- Multi-tenant configuration + operator docs

---

*Legend: ✅ done · everything else in flight or planned. Follow the
[issues]({{ site.repo_url }}/issues) for live status.*

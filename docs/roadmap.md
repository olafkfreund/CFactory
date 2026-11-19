---
layout: default
title: Roadmap
permalink: /roadmap/
---

# Roadmap

CFactory is built in phases, tracked as epics in the
[CFactory repo]({{ site.repo_url }}/issues) and rolled up in the
[Factory issue tracker](https://github.com/olafkfreund/Factory/issues).

Sequencing principle: **spine first**. CFactory is only as good as the pipeline it
observes, so the cross-repo PARR spine is standardized before the cockpit goes
deep.

## Phase 0 — Standardize the spine *(prerequisite, tracked in the Factory repo)*

- Wire PFactory → AIFactory inbound handoff with provenance
- Adopt a shared correlation key end-to-end (now repo-scoped, `<repo>#<n>`; see RFC-0001)
- Normalize the completion-event envelope across all three services
- Resolve the PFactory/TFactory port collision (canonical map, UI/API: AIFactory
  3100/3101, TFactory 3103 (API), PFactory 3114/3115, CFactory UI 3110 / API 3111)

## Phase 1 — Skeleton, correlation store & read-only cockpit *(shipped ✅)*

- Fork the shared Factory skeleton; ports 3110/3111; `~/.cfactory` workspace ✅
- `WorkItem` correlation model + PostgreSQL persistence ✅
- Service adapters: PFactory, AIFactory, TFactory REST clients ✅
- WebSocket live subscriptions ✅
- Webhook ingress (`POST /api/events`) ✅
- Read-only pipeline board UI ✅

## Phase 2 — Agentic pipeline copilot *(shipped ✅)*

- Claude Agent SDK copilot service
- Read tools: query WorkItems, summarise timelines, cost/latency rollups
- Anomaly detection: stuck phases, handback loops, gate failures, cost spikes
- Copilot chat panel + proactive insight cards

## Phase 3 — Advise + confirm actions, audit & scoped keys *(shipped ✅)*

- Prepared-action model (`propose_*`)
- Action tools: approve gate, trigger handoff, kick handback
- Human-in-the-loop confirm UI + full audit log
- Scoped API keys with read/write enforcement

## Phase 4 — Harden for hosted / multi-tenant *(shipped ✅)*

- Reuse AIFactory enterprise auth (SAML/SCIM, tenant isolation, HMAC audit)
- Docker image + Helm chart
- Multi-tenant configuration + operator docs

## Phase 5 — Live agent terminals *(shipped ✅)*

- Backend discovery of active AIFactory agents (`GET /api/live-agents`) ✅
- Read-only server-side proxy of each agent's rmux console
  (`WS /api/live-agents/{key}/ws`) — ANSI streamed to the browser, no upstream
  token ever exposed ✅
- xterm.js terminal tiles in Mission Control with a click-to-expand modal, plus
  loading / rmux-disabled / no-agents states ✅
- See [Live agents](https://github.com/olafkfreund/CFactory/blob/main/techdocs/live-agents.md)
  for the data path and the read-only guarantee.

## Phase 6 — Operate, not just observe *(shipped ✅)*

- **Running tasks** view — live progress across every sibling with
  `All / Running / Failed / Done` filters, per-task progress and current phase ✅
- Clickable **task detail drawer** — live process output plus the in-progress
  **rmux** terminal ✅
- **Audit** view — live completion-activity feed + a confirmed-actions log ✅
- **Services** panel — per-service health with **editable upstream endpoints**
  (repoint PFactory/AIFactory/TFactory without a redeploy) ✅
- Settle the canonical port map end-to-end (port remap) ✅
- Copilot defaults to **Claude Opus 5.5** (was Opus 4.8) ✅

## Phase 7 — Ship continuously (GitOps) *(shipped ✅)*

- Containerize the backend and the cockpit as separate images ✅
- **Two-pod Helm chart** + a `devenv` workflow for local iteration ✅
- CI: build and push sha-tagged images to **GHCR** on every push to `main` ✅
- CD: bump image tags in **factory-gitops** so **ArgoCD** redeploys the k3d
  cluster automatically ✅

## Cross-cutting — Token & cost instrumentation *(shipped ✅)*

- All three stages now emit the RFC-0001 `usage` block on completion
  (AIFactory, PFactory and TFactory) ✅
- The Tokens & cost page totals real per-service, per-work-item spend ✅

## Recently shipped — August to November 2026

CFactory has no releases or version numbers. "Shipped" here means merged to
`main`. As of 2026-11-17, `main` includes the promotion of everything merged to `dev` through
2026-11-16; check the merged list for the current head. See the
[merged pull requests](https://github.com/olafkfreund/CFactory/pulls?q=is%3Apr+is%3Amerged+base%3Amain),
and the post
[What shipped, August to November]({{ '/blog/2026/11/15/what-shipped-august-to-november/' | relative_url }}).

- **A cockpit that does not overstate** — a test lane that did not run no longer
  renders as passed
  ([#432](https://github.com/olafkfreund/CFactory/pull/432),
  [#437](https://github.com/olafkfreund/CFactory/pull/437)); the cockpit
  refetches after a websocket drop
  ([#443](https://github.com/olafkfreund/CFactory/pull/443)).
- **Stalled work stays on the board** — flagged after 900 s idle (default,
  `CFACTORY_STALL_DEADLINE_SECONDS`), deleted after 24 h (default,
  `CFACTORY_STALLED_PRUNE_HOURS`), running stages never pruned
  ([#483](https://github.com/olafkfreund/CFactory/pull/483)).
- **Correlation across the three services** — repo-scoped keys, and bare-number
  fallbacks removed
  ([#487](https://github.com/olafkfreund/CFactory/pull/487),
  [#506](https://github.com/olafkfreund/CFactory/pull/506)).
- **Access control, staged** — `/api` fails closed without keys; events auth and
  legacy MCP secret use are warn-only for now
  ([#500](https://github.com/olafkfreund/CFactory/pull/500),
  [#502](https://github.com/olafkfreund/CFactory/pull/502),
  [#504](https://github.com/olafkfreund/CFactory/pull/504)).
- **CI and supply-chain gates** — gitleaks, a blocking security-sink gate, and a
  weekly CodeQL ratchet at a zero baseline
  ([#384](https://github.com/olafkfreund/CFactory/pull/384),
  [#371](https://github.com/olafkfreund/CFactory/pull/371),
  [#494](https://github.com/olafkfreund/CFactory/pull/494)).

## Next

On `dev`, not yet on `main`: a model-gateway route for the copilot, off unless
`CFACTORY_MODEL_GATEWAY_URL` is set
([#513](https://github.com/olafkfreund/CFactory/pull/513)), and per-factory
scoped tokens, step 1 of a four-PR set
([#510](https://github.com/olafkfreund/CFactory/pull/510)). The `enforce` modes
for events auth and the legacy MCP secret exist but default to non-enforcing;
no date is set.

---

*Legend: ✅ done · everything else in flight or planned. Follow the
[issues]({{ site.repo_url }}/issues) for live status.*

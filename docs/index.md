---
layout: default
title: Home
---

# CFactory — the control tower for your software factory

**CFactory is the agentic cockpit that sits above the PARR pipeline.** It threads
every unit of work across three autonomous services — planning, execution and
verification — into one pane of glass, and adds an LLM copilot that explains what
is happening and proposes the next move.

> Three autonomous tools are powerful. Three autonomous tools you can *see,
> correlate and steer from one place* are a factory.

CFactory is the newest member of the **[Factory family](/family/)** — and the
piece that turns the others into a suite.

> **Part of the [Factory family](https://factory.freundcloud.com/)** — a governed, verified, observable autonomous software factory. [PFactory](https://pfactory.freundcloud.com/) plans · [AIFactory](https://aifactory.freundcloud.com/) builds · [TFactory](https://tfactory.freundcloud.com/) verifies · **CFactory** watches over all four. → **[Why Factory](https://factory.freundcloud.com/why/)**

---

## The problem it solves

The Factory family already plans, builds and verifies software autonomously:

```
PFactory  →  AIFactory  →  TFactory
 (Plan)        (Act)       (Reflect/Review)
```

But each runs on its own portal, its own workspace, its own status model. Nobody
can answer the obvious question — **"where is feature X right now, and why is it
stuck?"** — without opening three tabs and stitching the story together by hand.

CFactory answers it. It is the **observability + control layer** the modern
agentic SDLC needs.

## What CFactory does

- **Threads work across services.** A single `WorkItem`, keyed by GitHub issue,
  follows the chain `plan → code → branch/PR → tests` so you always know where
  something is.
- **One live cockpit.** A read-first board shows every WorkItem across the
  plan / code / test stages with status, gates, verdicts and PR links — fed by
  REST, WebSocket and webhooks from the three services.
- **An agentic copilot.** Ask "why is #182 stuck?" and get an answer grounded in
  real cross-service state. The copilot summarises timelines, computes cost and
  latency rollups, and flags anomalies (stuck phases, runaway handback loops,
  gate failures, cost spikes).
- **Advise + confirm, never silent.** The copilot can *prepare* actions — Approve
  a plan gate, Approve review (accept code, open the PR, then merge), Reject,
  Recover, or Remove a failed task — but every write waits for an explicit human
  click and is audited. Human-in-the-loop by design.
- **A self-cleaning board.** Stuck and stalled cards no longer linger as phantom
  "running" tasks: a stage an upstream reports as `stalled` is filtered from the
  live feed and pruned, and a silent *running* frontier past the stall deadline
  is age-pruned each poll cycle. A `*_review` or queued frontier waiting on a
  human is never touched.
- **Watch agents work, live.** When a build is running, the cockpit streams each
  AIFactory agent's terminal straight into Mission Control — a read-only window
  into what the agent is doing *right now*, no extra tabs. The LIVE AGENTS panel
  also surfaces TFactory verify sessions, so an active verify never reads as idle.
- **See the cost.** Every stage reports token usage and cost via the shared
  RFC-0001 `usage` block, so the Tokens & cost page totals real spend across
  plan, code and test — per work item and per service.

## Where it fits

| Stage | Product | Role |
|------|---------|------|
| **Prepare / Plan + Review** | [PFactory](https://pfactory.freundcloud.com/) | Governed, context-grounded planning |
| **Act** | [AIFactory](https://aifactory.freundcloud.com/) | Spec-first plan → code → QA execution |
| **Reflect / Review** | [TFactory](https://tfactory.freundcloud.com/) | Autonomous test generation + 5-signal verdict |
| **Observe / Steer** | **CFactory** | The cockpit over all three |

[See the architecture →](/architecture/) &nbsp; · &nbsp; [See the roadmap →](/roadmap/) &nbsp; · &nbsp; [Meet the family →](/family/)

**Plan the work:** the [Planning board guide](guides/planning-board.md) — the
agent-native backlog RFC-0019 added in front of the pipeline: cards, board views,
MCP board tools and scopes, and how promoting a card to `ready` dispatches it
into the factory by itself. New here? Follow
[Plan a card, watch it build](guides/plan-a-card-walkthrough.md) end to end.

**See the cockpit:** the [Cockpit gallery](guides/cockpit-gallery.md) — a captioned tour of every view, captured against the live cluster, including the live execution graph across all three PARR stages.

**Connect your editor:** see [Connecting editors & external clients](guides/token-gated-api.md) — how to point VS Code (and other clients) at CFactory in any deployment.

**Design system:** the [Factory Design System](guides/factory-design-system.md) — the shared brand & UI rules every Factory service follows so the suite looks like one product.

**Multi-tenant mode:** [Multi-Tenant Mode](guides/multi-tenant.md) — the `CFACTORY_MULTI_TENANT` flag, how `X-Tenant-Id` is resolved, and the operator flip steps.

**GitHub sync:** [GitHub Card ↔ Issue Sync](guides/github-card-sync.md) — how a planning card is backed by a GitHub issue, and the conflict rule: GitHub is the record of truth, so on conflict **GitHub wins**.

**Connecting a git host:** [Registering the GitHub App and the GitLab OAuth application](guides/git-app-install.md) — the operator runbook for the install flow: which permissions to grant, which events to leave off, where the callback is hosted and why, and what to do with the App private key. The one part of the flow no software can do for you.

**Configuration:** the [Environment reference](dev/environment-reference.md) — every environment variable, flag and operational parameter CFactory reads, with defaults and which ones must be set for a hosted deployment.

---

<p class="muted">CFactory is early and built in the open. Follow along on
<a href="{{ site.repo_url }}">GitHub</a> or read the <a href="/blog/">blog</a>.</p>

---
layout: default
title: Factory Family
permalink: /family/
---

# The Factory family

The Factory family is a set of composable, open products that together form a
**governed, verified, observable autonomous software factory**. Each follows the
**PARR** loop — Prepare, Act, Reflect, Review — and each is useful on its own, but
they're designed to hand off to one another.

```
PFactory  →  AIFactory  →  TFactory        … all observed and steered by …  CFactory
 (Plan)        (Act)        (Verify)                                          (Cockpit)
```

## The members

### 🧭 PFactory — *Prepare / Plan + Review*
The planning & governance layer that sits **in front of** coding agents. It ingests
plans, enriches them with **live** organizational context (cloud, Backstage, infra),
runs architecture/security/feasibility review gates with citations, records human
approval, and emits governed GitHub issues.
→ [pfactory.freundcloud.com](https://pfactory.freundcloud.com/) ·
[GitHub](https://github.com/olafkfreund/PFactory)

### 🛠️ AIFactory — *Act*
The execution engine. Spec-first plan → code → QA, multi-provider (and able to
*delegate* to GitHub Copilot / GitLab Duo), enterprise-grade (SAML/SCIM, tenant
isolation, audit, LiteLLM gateway). Produces merge-ready branches and PRs.
→ [aifactory.freundcloud.com](https://aifactory.freundcloud.com/) ·
[GitHub](https://github.com/olafkfreund/AIFactory)

### 🧪 TFactory — *Reflect / Review*
Autonomous test generation + execution. It grades generated tests on a **5-signal
verdict** — coverage delta, stability re-runs, mutation kills, lint, semantic
relevance — so you get tests you can *trust*, not just a green bar. Hands
corrections back to AIFactory.
→ [tfactory.freundcloud.com](https://tfactory.freundcloud.com/) ·
[GitHub](https://github.com/olafkfreund/TFactory)

### 🛰️ CFactory — *Observe / Steer*  **(you are here)**
The agentic control tower. One pane of glass that threads work across all three
services, plus an LLM copilot that explains pipeline state and proposes
human-confirmed actions.
→ [GitHub](https://github.com/olafkfreund/CFactory)

### 🏭 Factory — *the program repo*
The central repository for cross-cutting plans and epics that span the whole
family, and the home of the shared PARR pipeline.
→ [GitHub](https://github.com/olafkfreund/Factory)

---

## Why a family, not a monolith

Each product is independently useful and independently deployable — you can adopt
TFactory for QA without the rest, or PFactory for governed planning in front of any
coding agent. But run together and observed through CFactory, they become something
the market hasn't quite seen: an autonomous software factory you can **govern,
verify and watch** end to end.

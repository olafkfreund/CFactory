# CFactory

**The agentic control-tower cockpit over the autonomous software factory.**

CFactory is the **Observe / Steer** layer of the [Factory](https://factory.freundcloud.com/)
PARR pipeline. It is a read-first, advise-and-confirm cockpit layered over the three
Factory services — **PFactory** (Plan), **AIFactory** (Act) and **TFactory** (Verify).
It owns no pipeline logic of its own: it observes, correlates, and — with a human
click — triggers the services through their existing APIs.

## What it does

- **Ingest** a normalized completion-event envelope (RFC-0001) from all three services
  via a `POST /api/events` webhook, plus live WebSocket subscriptions and on-demand
  REST polling.
- **Correlate** every unit of work into a single **WorkItem**, keyed by the GitHub
  issue number, that threads `plan → code → test` with a full event timeline.
- **Insight** — board rollups (counts · latency · token/cost), and anomaly detection
  for stuck phases, handback loops and gate/test failures.
- **Copilot** — a Claude Agent SDK layer that answers "where is feature #142 and why
  is it stuck" over the live board snapshot. Read-only; never writes.
- **Advise + confirm** — propose an action (`approve_gate`, `trigger_handoff`,
  `kick_handback`), review it, then explicitly confirm to execute. **No autonomous
  writes**; every confirmed action lands in a tamper-evident audit log.

## Why it matters

The Factory family lacked a shared identity for a unit of work and a single place to
watch it flow. CFactory introduces both: one pane of glass plus an LLM copilot that
explains pipeline state and proposes human-confirmed actions — without ever wresting
control from the operator.

## Where it fits

```
PFactory ──▶ AIFactory ──▶ TFactory        … all observed and steered by …  CFactory
 (Plan)        (Act)        (Verify)                                          (Cockpit)
```

Ports: the cockpit UI on **3110**, the backend API on **3111**.

See [Architecture](architecture.md), [Dependencies](dependencies.md),
[Decisions](decisions.md) and [API & WebSocket](api.md).

---
layout: post
title: "The cockpit learns to show live, per-worker cost"
subtitle: "A day of work took the Tokens & cost page from a per-service total to a per-worker drill-down, then put a live, ticking cost stamp and a cumulative sparkline on every running task — all additive, all gated on a real test run."
date: 2026-06-13 09:00:00 +0000
author: Olaf Freund
---

When we [introduced CFactory](/blog/2026/06/04/introducing-cfactory/) the cost
story was simple: every service attaches the shared RFC-0001 `usage` block to its
completion events, and the cockpit totals real LLM spend per service and per work
item. That answered "what did this feature cost to plan, build and verify?".

It did not answer the next two questions an operator actually asks while a build
is running: *which worker is burning the budget right now*, and *is the cost still
climbing*. Today's work closes both gaps — and does it without changing a single
byte of how old events are ingested.

## First, a real test gate (so the rest of this could ship safely)

Before adding features, we fixed the thing underneath them. CFactory shipped to
ArgoCD on every push to `main` with **no pre-merge test gate** — the deploy
trusted that whatever merged was green. [PR #82](https://github.com/olafkfreund/CFactory/pull/82)
added a `test` workflow that runs the full **224-test backend pytest suite** plus
a frontend TypeScript typecheck and production build, and made the deploy workflow
**depend on it**. Nothing reaches the cluster now unless the tests pass first.

The same PR closed a latent security hole: a startup guard that **refuses to boot
with the default audit-HMAC secret** outside local mode. The audit log is
HMAC-anchored to be tamper-evident; shipping the well-known default secret to
production would have quietly defeated that. It now fails loudly instead.

Everything below was built on top of that gate. Each feature PR had to pass the
**Backend pytest** check to merge.

## Per-worker ingest: from a service total to a worker drill-down

A "task" in AIFactory is not one agent — it is a fan-out of workers, each on its
own slice, often on different providers and models. The old `usage` rollup
flattened that into one number per service. [PR #86](https://github.com/olafkfreund/CFactory/pull/86)
unflattens it.

The `WorkItem` model gained a `WorkerUsage` list and two derived rollups,
`by_provider` and `by_model`, and a new endpoint, `GET /api/tokens/by_worker`,
serves them. The Tokens & cost page now has a per-worker drill-down and a
per-provider rollup, so "which worker, on which model, cost what" is a click, not
a guess.

The important property is that this is **idempotent by `worker_id`** and purely
additive: a worker sub-event upserts into the `workers` map, and **events that
carry no worker data ingest exactly as before**. Old events, and services that
never emit per-worker detail, are unchanged.

## A soft-budget badge and a link to the metrics UI

[PR #87](https://github.com/olafkfreund/CFactory/pull/87) added a small but useful
signal: an **"over budget" badge** that renders *only* when a work item's
`usage.budget.exceeded` flag is set. It is deliberately soft — informational, not
a blocker — because the budget is advisory and the cockpit's job is to surface,
not to enforce. The same PR added a **configurable nav link to the OpenObserve
UI**, which matters for the architectural decision below.

## A live, ticking per-task cost stamp — and a sparkline

The headline of the day. [PR #88](https://github.com/olafkfreund/CFactory/pull/88)
put a **live stamp on every running task card**: accumulated cost, tokens,
workers-done, and elapsed time, all updating in place. Next to it is a
**hand-rolled SVG sparkline of cumulative cost** that steps up as each worker
finishes. No charting dependency — just an SVG path driven by the data the cockpit
already has.

It rides the **existing WebSocket broadcast plus poll** — no new transport — and
it is fully additive: a card with no worker data renders exactly as it did before.
You opt into the richer view by having the data, not by changing the code path.

## Smooth ticking: per-worker progress heartbeats

The stepwise sparkline jumps once per worker. For a long build that is a coarse
signal. [PR #89](https://github.com/olafkfreund/CFactory/pull/89) makes it tick
smoothly by ingesting throttled `phase:"worker_progress"` **heartbeats** into a
rolling per-worker series, exposed at
`GET /api/tasks/{key}/worker-progress`, which feeds a **dense** cumulative series
into the sparkline for a ~10s tick.

The heartbeats are a high-frequency *stream*, so they are handled differently from
terminal events: the series is **capped at 120 points per worker** (about 20
minutes at a 10s cadence) and **pruned entirely on a terminal event** — the
detail is only needed while a task is running, so the store cannot bloat. When no
heartbeats arrive, the sparkline **falls back to the stepwise per-worker series**.
Both paths are tested.

## The decision worth spelling out: where per-task detail comes from

It would be reasonable to assume all of this live, per-task data comes from our
metrics backend. It does not, and that is deliberate.

We run **OpenObserve** as a bundled sibling app for fleet-wide observability (the
cockpit now links to it, from PR #87; the backend wiring is in factory-gitops
[PR #49](https://github.com/olafkfreund/factory-gitops/pull/49), still pending).
The OpenTelemetry metrics the services emit are intentionally **low-cardinality** —
notably **no `task_id`** — because high-cardinality labels are what make a metrics
TSDB fall over. That makes OpenObserve excellent for fleet-wide aggregates and
useless for "show me *this one running task*".

So the two systems split the work cleanly:

- **OpenObserve** serves fleet-wide aggregates over low-cardinality metrics.
- **CFactory** serves the per-running-task drill-down — accumulated cost, the
  worker breakdown, the live sparkline — from **its own event store**, the same
  worker events it already ingests over the webhook and WebSocket surfaces.

And a non-goal worth stating: CFactory does **not** implement an OTLP receiver or
a time-series database of its own. That would be reinventing Grafana, Tempo and
the rest. The per-task detail is a bounded, capped, pruned slice of the event
store that already backs the cockpit — not a parallel metrics pipeline.

## Why it matters

A per-service total tells you a build was expensive after it finished. A live,
per-worker stamp with a cumulative sparkline tells you *which* worker is expensive
*while it runs*, on *which* model, and whether the curve is still climbing — early
enough to act. Every piece of it is additive, gated on a real test run, and
sourced from the right system: the cockpit for per-task drill-down, OpenObserve
for the fleet.

CFactory is built in the open. Follow along on
[GitHub](https://github.com/olafkfreund/CFactory), or see the
[architecture](/architecture/) and [roadmap](/roadmap/) for where it goes next.

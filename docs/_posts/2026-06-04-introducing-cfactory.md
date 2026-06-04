---
layout: post
title: "Introducing CFactory: a control tower for your software factory"
subtitle: "Three autonomous tools are powerful. Three you can see, correlate and steer from one place are a factory."
date: 2026-06-04
author: Factory Team
---

The Factory family already does something remarkable: it plans software
([PFactory](https://pfactory.freundcloud.com/)), builds it
([AIFactory](https://aifactory.freundcloud.com/)), and verifies it
([TFactory](https://tfactory.freundcloud.com/)) — autonomously, with humans in the
loop only where it matters.

But there was a gap, and it was the obvious one. Each service runs on its own
portal, its own workspace, its own status model. To answer a simple question —
*"where is this feature right now, and why is it stuck?"* — you had to open three
tabs and reconstruct the story by hand. The pipeline was real; the **view** of it
wasn't.

CFactory is that view.

## A cockpit, not another dashboard

CFactory threads every unit of work across the three services into a single
`WorkItem`, keyed by its GitHub issue number:

```
plan  →  code  →  branch / PR  →  tests
```

One board shows where everything is — with gates, verdicts and PR links — fed
live by REST, WebSocket and webhooks from the three services. No new agent logic;
CFactory observes through the APIs that already exist.

## And a copilot that actually knows the pipeline

On top of the board sits an agentic copilot (built on the Claude Agent SDK). Ask
it "why is #182 stuck?" and it answers from real cross-service state — summarising
the timeline, rolling up cost and latency, and flagging anomalies like runaway
handback loops or cost spikes.

Crucially, it's **advise-and-confirm**. The copilot can *prepare* an action —
approve a gate, trigger a handoff, kick a stalled handback — but nothing is written
until a human clicks. That's the human-in-the-loop "Red Zone" guardrail the whole
family is built around.

## Built like family

CFactory shares the family skeleton — Python 3.13 + FastAPI, React 19 + Vite,
PostgreSQL, a Nix dev shell — and reuses AIFactory's enterprise auth so security
and operations match from day one.

We're building it in the open, spine-first. Follow along on
[GitHub](https://github.com/olafkfreund/CFactory) and on the
[roadmap](/roadmap/).

---
layout: post
title: "Watch the agents, and see the cost"
subtitle: "Two updates land in the cockpit: live agent terminals streamed into Mission Control, and real token spend across all three stages."
date: 2026-06-05
author: Factory Team
---

When we [introduced CFactory](/blog/2026/06/04/introducing-cfactory/), the pitch
was simple: turn three autonomous tools into a factory you can *see, correlate
and steer from one place*. Two updates this week push that further — one about
**seeing**, one about **cost**.

## Live agent terminals

A status dot tells you AIFactory is "coding". It doesn't tell you *what* it's
doing. So now, when a build is running, the cockpit streams each agent's terminal
straight into Mission Control.

Under the hood, AIFactory already runs its agents under **rmux**, a server-side
terminal multiplexer that exposes a per-task console. CFactory's backend now lists
the active agents (`GET /api/live-agents`), opens each console WebSocket
**server-side**, and re-streams the raw ANSI bytes into an
[xterm.js](https://xtermjs.org/) tile in the browser. Click a tile to expand it
to a full-size terminal.

Two properties we cared about:

- **Read-only by design.** The cockpit observes; it never attaches to a session
  or forwards keystrokes. You're watching, not driving.
- **Single-origin and token-safe.** The browser talks only to CFactory. The
  AIFactory URL and service token never leave the backend — the proxy holds them.

It degrades honestly, too: if rmux is off upstream or nothing is running, the
panel says so instead of spinning. The full data path and the read-only guarantee
are written up in the
[Live agents](https://github.com/olafkfreund/CFactory/blob/main/techdocs/live-agents.md)
docs.

## Real cost, across all three stages

The Tokens & cost page used to be honest but thin: every stage showed "not
instrumented yet", because none of the three services actually attached cost to
their completion events. That gap is closed.

All three services now attach the shared **RFC-0001 `usage` block** — input and
output tokens, cost, and model — to their completion events. PFactory sums the
usage of its planning LLM calls; TFactory accumulates across its test sessions
*and* handback retries; AIFactory maps the per-task token tracking it already
kept internally into the event. CFactory aggregates the lot into per-service and
per-work-item totals.

The result: when you ask "what did feature #182 cost to plan, build and verify?",
the cockpit has a real number — not three tabs and a guess.

## Why it matters

Both updates are the same idea from different angles. An autonomous pipeline that
you can't watch and can't cost isn't a factory you'd trust with real work. Now you
can watch the agents as they go, and see what each unit of work actually spent —
from one pane of glass.

CFactory is built in the open. Follow along on
[GitHub](https://github.com/olafkfreund/CFactory).

---
layout: post
title: "The cockpit stops lying to you"
subtitle: "This cycle was about honesty: a board that prunes its own phantom tasks, a live-agents panel that admits when a verify is running, human-in-the-loop actions that never write without a click, and one page that documents every knob."
date: 2026-07-23 12:00:00 +0000
author: Factory Team
---

A control tower is only as good as the state it shows. A dashboard that claims a
task is "running" when it died an hour ago, or reads "no agents running" while a
verify grinds away, is worse than no dashboard — it teaches operators to distrust
the one pane of glass they are supposed to steer from. This cycle of CFactory was
about closing that gap between what the cockpit shows and what is actually true.

## A board that cleans itself

Two classes of card used to linger forever. The first: a stage an upstream
service reports as `stalled` — its own liveness watchdog gave up on a hung step.
That status matched none of our status buckets, so the cockpit fell back to
showing it as "running" indefinitely, and because the producer kept listing it,
it never reconciled away. The second, subtler one: a stale `in_progress` frontier
orphaned from any completion event — nothing to match on, no `stalled` string to
filter, so it showed as running for good.

Both are gone. Every poll cycle the cockpit now filters `stalled` stages out of
the live feed and prunes any that already reached the board, and it age-prunes a
silent *running* frontier once it passes the stall deadline
(`CFACTORY_STALL_DEADLINE_SECONDS`, 900s by default). The important guard: a
frontier parked in `*_review` or queued is legitimately *waiting on a human* and
is never pruned. Only a silent running task is a dead task.

## A live-agents panel that tells the truth

The LIVE AGENTS panel used to discover AIFactory rmux sessions and nothing else.
So during an active TFactory verify — real agent work, one of three pipeline
stages — it read "no agents running", making a busy factory look idle. It now
unions two sources: AIFactory sessions, which are streamable with a terminal
console, and TFactory verify sessions, shown as informational rows without a
console button. The empty state is honest too: "No agent sessions running right
now" instead of overclaiming idle across every stage.

## Actions that wait for you

The copilot can be helpful without being autonomous. It *prepares* an action —
Approve a plan gate, Approve a review (accept the code, open its pull request,
then merge), Reject, Recover, or Remove a failed task — and then stops. Nothing
touches an upstream service until an explicit human click, and every executed
write is written to the tamper-evident audit chain. Remove works even on a failed
task; plan-stage tasks have no delete endpoint upstream, so Remove is disabled
there rather than failing silently.

## One place for every knob

Finally, the unglamorous but load-bearing work: a single
[environment reference](/dev/environment-reference/) that documents every
environment variable, flag and operational parameter the cockpit reads — backend
settings, the copilot's provider credentials, the frontend build-time bundle, and
the nginx container runtime — each with its default and whether a hosted
deployment must set it. A fresh operator now needs one page and the
`.env.example`, not a source dive.

None of this adds a headline feature. All of it makes the cockpit something you
can trust at 3am, which is the only time a control tower actually matters.

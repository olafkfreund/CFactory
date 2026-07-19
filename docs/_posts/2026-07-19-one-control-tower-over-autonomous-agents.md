---
layout: post
title: "One control tower over autonomous agents"
subtitle: "A plain GitHub issue went in and a tested pull request came out with no human in the loop. This is that run watched from the cockpit — the pipeline strip, the event feed, the live agent terminals, the review queue, and the bill."
date: 2026-07-19 12:00:00
author: Factory Team
---

The Factory is four cooperating services: PFactory plans, AIFactory builds,
TFactory tests, and CFactory watches all three. On 2026-07-19 we filed a plain
issue against a demo repo and let the fleet run it unattended — plan, build in an
ephemeral Kubernetes Job, open its own pull request, generate and run tests, and
grade the result. Zero humans in the loop. This post is that run seen from the
cockpit, because a pipeline you cannot watch is one you cannot trust.

## One correlation, three stages

The featured task was small on purpose: add a `clamp(value, low, high)` helper to
the demo repo. Small task, full pipeline. The cockpit's job is to thread that one
piece of work through PFactory, AIFactory, and TFactory as a single correlation —
so plan, build, and test are three stages of one thing on the board, not three
disconnected events you have to line up by hand.

The live pipeline strip shows the shape at a glance: plan counts, build counts,
test counts, moving as the run progresses. The event feed underneath is the run's
real completions — not a synthetic progress bar, the actual events each service
emitted as it finished a stage. When PFactory signed the plan, when AIFactory's
Job opened PR #387, when TFactory returned a verdict: each one lands in the feed
as it happens.

## Watching the agents work

Every running agent streams its terminal into the portal and into Mission Control
as it works. You are not reading a log after the fact; you are watching the coder
think and the tester run, live, with parallel agents visible on one board. That
matters for autonomous work in a way it does not for a CI job — when there is no
human writing the code, the terminal is the only window into what the machine is
actually doing, and the cockpit keeps that window open the whole time.

## The number that has to be true

The cockpit is billing-aware. Each stage stamps the model tier it ran at into its
completion event, and the cockpit surfaces token counts and cost per task. You can
look at a finished run and see that planning used a frontier model, the bulk work
used a cheaper one, and what the whole thing cost. A saving you cannot see is a
saving you cannot defend.

For this run the verdict was clean: verified to VAL-1, all five acceptance
criteria met, nine tests generated and kept, zero rejected, the mutation probe
killed, confidence 0.96, stable across three runs. VAL-2 and VAL-3 were reported
`not_run` — correctly, because no API, integration, or browser lane applies to a
pure function. An untested dimension is an honest gap, never a silent pass, and
the cockpit shows it as a gap rather than dressing it up as green.

## The slugify story

Minutes before the clean clamp run, the fleet built a `slugify` helper from a
code-aware plan session. It compiled and looked fine — and failed one of twelve
test verdicts on a unicode edge case. The never-overclaim gate capped it at VAL-0
and auto-filed a handback to fix it. It refused to certify a build with a failing
test. On the board that shows up as a run that stopped short and asked for a fix,
not a green checkmark over a broken assertion. That is the capability, not a bug:
tests that refuse to lie, and a cockpit that shows you when they do.

## The queue, and the honest edge

When a run needs a person, it lands in the human-review queue — the one place an
operator looks to see what is waiting on a decision instead of scanning every
task. This run surfaced its own rough edge there too: the verify verdict is
computed correctly, but its auto-post back onto the pull request is gated by a fix
we are now tracking as an issue. The factory found the last loose end in its own
feature and said so. We would rather name it than let the board imply a polish the
run does not yet have.

## Why a control tower

Autonomous agents are only as trustworthy as your ability to see what they did.
The cockpit is the single pane of glass over the PARR pipeline: one correlation
across three services, a live strip of where the work is, a feed of what actually
finished, terminals into every running agent, the real cost, and an honest
verdict — including the gaps. An operator should never have to guess what the
factory just did, what it cost, or whether to believe it.

## Watch it run

One continuous walkthrough of all four live portals with this run's own data:

<video controls preload="metadata" style="width:100%;max-width:960px;border-radius:8px" src="{{ '/assets/blog/2026-07-19/factory-walkthrough.mp4' | relative_url }}">
  Your browser does not support embedded video. <a href="{{ '/assets/blog/2026-07-19/factory-walkthrough.mp4' | relative_url }}">Download the walkthrough</a>.
</video>

---
layout: post
title: "If you cannot see it, you cannot trust it"
subtitle: "The cockpit's job this cycle: make the cost of a run, the model tier each stage used, and the difference between a real success and a silent no-op something an operator can actually watch."
date: 2026-07-13 12:00:00
author: Factory Team
---

CFactory is the cockpit over the whole Factory pipeline: the place an operator
watches PFactory plan, AIFactory build, and TFactory verify, and steps in where a
human decision is needed. The theme of this cycle across the fleet was honesty,
and honesty only counts if someone can see it. That is the cockpit's job.

## Why visibility is the point

Two things happened this cycle that only matter because you can watch them.

The first is cost-aware routing. The fleet now routes each stage to an
appropriately sized model, and on a measured run that cut cost by 55 percent at
the same amount of work. A saving you cannot see is a saving you cannot defend, so
the tier each worker ran at is now stamped into the completion event and surfaced
per task. You can look at a run and see that planning used a frontier model and
the bulk coding used a cheaper one, and you can see the bill that resulted.

The second is the opposite of a feature: a failure that used to hide. The
benchmark showed that nearly half of the coder's failures produced no code at
all, and the pipeline used to report some of those as complete. A build that
quietly did nothing now surfaces as failed rather than green. The cockpit's
contract with the operator is that a success on the board is a real success, and
this cycle tightened that contract.

## What this proves

That we treat observability as part of the product, not decoration on top of it.
The reason an operator can trust an autonomous factory is not that it never
fails. It is that when it succeeds you can see why, when it spends you can see
what on, and when it fails it says so plainly. A cockpit that shows the cost, the
routing decision, and the honest verdict is what turns a black box into something
a team can actually run in production.

## What is next

Push more of the fleet's new signals to the surface: routing tier and cost
savings per stage, the security and verification verdicts that gate progress, and
a clear reason whenever a run needs a human. The measure of the cockpit is simple.
An operator should never have to guess what the factory just did, what it cost, or
whether to believe it.

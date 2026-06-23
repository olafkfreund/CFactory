---
layout: post
title: "Every requirement, its test, in one table"
subtitle: "The cockpit now answers the question a reviewer actually has before they approve a build: is every acceptance criterion actually tested, and how far was it verified? A traceability matrix renders acceptance-criterion x test x VAL x verdict in one pane, and an AC with no covering test is flagged as a gap rather than left quietly blank."
date: 2026-06-23 12:00:00 +0000
author: Olaf Freund
---

The honest question a reviewer asks before signing off on a build is not "did the
tests pass?" It is "was *everything* tested?" A green suite that silently skips a
requirement is more dangerous than a red one, because it reads as done. The cockpit
now answers the real question, in one table.

## The matrix

The task-detail drawer has a **Traceability** panel. It renders the
`verification.traceability[]` block as a grid with one row per **acceptance
criterion**, and four things across:

- the **test(s)** that cover that criterion,
- the **VAL** — the Verification Assurance Level the result reached, i.e. how far it
  was actually verified, not just whether it ran,
- the **verdict** — pass, fail, not_run, or skipped.

The row that matters most is the one with no test against it. An acceptance criterion
with **no covering test is flagged as a gap** — called out, not rendered as an empty
cell you might skim past. "Untested" is a finding, so the cockpit treats it as one.

## Where the rows come from

The cockpit does not invent this. It mines the matrix from the upstream payloads it
already fetches on `GET /api/workitems/{key}/process`: the **TFactory** test detail
carries the traceability, with the test slice's stored `verification` block as a
fallback, and **PFactory**'s plan session carries the human-readable spec, plan, and
tasks. A companion **Artifacts** panel shows those spec/plan/tasks documents as tabbed
prose, so a reviewer reads the plan as docs and then checks it against the matrix
without leaving the drawer.

It degrades cleanly, which for a control tower is a feature, not a footnote. A
plan-or-code-only task that never reached verify shows nothing. An absent or empty
matrix shows a muted "not available" note — never an empty table, never a crash. The
cockpit would rather say "I don't have this yet" than imply full coverage it cannot
prove.

## The cockpit's job is to consume honesty, not manufacture it

This lands the same week PFactory and AIFactory started emitting cost as a **running**
figure — a usage snapshot the moment a plan is parked for approval or a build is in
flight, not only when work terminates. The cockpit records usage from any completion
event that carries it, terminal or not, so a plan awaiting a human no longer shows up
as a free `$0`. And the cost it shows stays honest about its own units: a real `$`
only for metered billing, tokens and wall-clock for subscription and local runs.

That is the throughline. PFactory governs and prices the plan; AIFactory builds and
prices the build; TFactory verifies and grades it. The cockpit's job is to put those
three honest signals — what was planned, what it cost, and whether every requirement
was actually tested — on one pane of glass, and to flag the gaps rather than paper over
them. A traceability matrix that names the untested criterion is exactly that job done.

See the [architecture guide](/architecture/) for how the cockpit assembles a task's
detail from the three services.

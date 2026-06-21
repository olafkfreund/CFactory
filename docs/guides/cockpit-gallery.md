---
layout: default
title: Cockpit gallery
permalink: /guides/cockpit-gallery/
---

# Cockpit gallery

A captioned tour of the CFactory cockpit, captured against the live cluster on
21 June 2026. Every shot is real data from the running control tower -- real work
items, real failures, real in-flight runs. For the story behind this set, see the
blog post [The cockpit shows the graph, the mode, and the logs](/blog/2026/06/21/the-cockpit-shows-the-graph-the-mode-and-the-logs/).

## Mission Control

The landing view. The PARR pipeline strip across the top tracks plan to code to
test from every view; below it are the work-item, event and anomaly counts, the
anomalies panel (here listing the three failed runs), the live agents row, and a
live event feed.

![Mission Control -- pipeline strip, counts, anomalies, live agents and event feed](/assets/blog/2026-06-21/01-mission-control.png)

A cleaner full-page view of the same screen:

![Mission Control, full view](/assets/blog/2026-06-21/15-overview-hero.png)

## Pipeline board

The plan / code / test columns. Each card is a work item; click one to open its
detail.

![The Pipeline board -- plan, code and test columns of work-item cards](/assets/blog/2026-06-21/02-pipeline-board.png)

Expand the Finished section and the done, failed and in-review items join the
board.

![The Pipeline board with the Finished section expanded](/assets/blog/2026-06-21/03-pipeline-with-finished.png)

## Task detail and the live execution graph

Open a work item and the detail view draws the live execution graph for the
engaged stage -- hand-rolled SVG over node cards, pannable and zoomable, in the
gruvbox stage palette. The PARR stage strip at the top shows all three lanes;
below the graph are the in-factory actions and a live terminal panel.

![A completed LinkLite plan -- acceptance-criteria nodes feeding a CI/CD setup node, with the PARR stage strip and actions](/assets/blog/2026-06-21/04-task-detail-linklite-dag.png)

Scrolled to surface more of the graph and the process detail:

![The same task detail scrolled down](/assets/blog/2026-06-21/05-task-detail-dag-scrolled.png)

A successful, completed run:

![A successful run's detail view](/assets/blog/2026-06-21/06-task-detail-success.png)

A failed run -- the test stage marked Failed, the upstream reason shown verbatim,
with unstick and remove controls:

![A failed task's detail view](/assets/blog/2026-06-21/07-task-detail-failed.png)

## Active tasks

The live floor of the factory: every in-flight run, filterable by state, with
per-stage progress. Most work items are awaiting review rather than actively
running, and the view says so.

![The Active tasks view -- in-flight runs with per-stage progress](/assets/blog/2026-06-21/08-active-tasks.png)

An in-progress task opened from the active view, its test stage still evaluating:

![An in-progress task's detail view](/assets/blog/2026-06-21/09-active-task-detail.png)

## Tokens and cost (billing-mode aware)

LLM usage across the pipeline. The page tells metered runs (real per-token
dollars) apart from subscription and local runs (reported in tokens and time,
not dollars). Each factory stage gets its own card; the per-work-item table
breaks spend down by task. Per-worker token observability is still being
completed upstream, so freshly run tasks may read "no token usage recorded yet".

![The Tokens and Cost page -- metered total, input and output, and per-stage cards](/assets/blog/2026-06-21/10-tokens-usage.png)

## Audit

The signed, HMAC-chained audit trail of live activity and confirmed factory
actions.

![The Audit view](/assets/blog/2026-06-21/11-audit.png)

## Services

Cockpit and upstream factory health, with editable endpoints.

![The Services view](/assets/blog/2026-06-21/12-services.png)

## Settings

Copilot provider and editor-token settings.

![The Settings view](/assets/blog/2026-06-21/13-settings.png)

## Copilot

The floating copilot assistant -- insights and chat over the live pipeline.

![The Copilot assistant](/assets/blog/2026-06-21/14-copilot.png)

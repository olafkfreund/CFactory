---
layout: post
title: "What shipped in CFactory, August to November 2026"
subtitle: "A cockpit that does not overstate, stalled work that stays visible, work joined up across the three services, and access control that is still mostly warn-only."
date: 2026-11-15 09:00:00
author: Factory Team
---

CFactory has no releases or version numbers, so this post uses dates and pull
requests. "Shipped" means merged to `main`. As of 2026-11-15, `main` ended at the
[#516](https://github.com/olafkfreund/CFactory/pull/516) sync (check the list below for the
current head), and everything merged on 2026-11-14 is on `dev` only, which is listed at the end.
The full list is the
[merged pull request list](https://github.com/olafkfreund/CFactory/pulls?q=is%3Apr+is%3Amerged+base%3Amain).

## A cockpit that does not overstate

- A test lane that did not run no longer renders as passed. The badge shows
  `Browser (0/8 run)` for an errored or pending lane, and STAGE COMPLETE needs
  at least one executed lane and no failed lane
  ([#432](https://github.com/olafkfreund/CFactory/pull/432),
  [#437](https://github.com/olafkfreund/CFactory/pull/437), shipped by
  [#433](https://github.com/olafkfreund/CFactory/pull/433)). If TFactory sends
  no `lane_progress`, the display is unchanged.
- One null `reason` in a TFactory verification level used to blank Mission
  Control with a raw schema error. It now parses, and a wrong type still fails
  ([#429](https://github.com/olafkfreund/CFactory/pull/429), shipped by
  [#430](https://github.com/olafkfreund/CFactory/pull/430)).
- After a websocket drop the cockpit refetches state on reconnect, instead of
  showing old data under a green "live" pill
  ([#443](https://github.com/olafkfreund/CFactory/pull/443), shipped by
  [#444](https://github.com/olafkfreund/CFactory/pull/444)).
- A stage is judged stuck from its current status, aged from its last real
  transition. Queued stages are never flagged stuck, and a discarded stage no
  longer raises a failure anomaly
  ([#455](https://github.com/olafkfreund/CFactory/pull/455),
  [#459](https://github.com/olafkfreund/CFactory/pull/459), shipped by
  [#456](https://github.com/olafkfreund/CFactory/pull/456) and
  [#460](https://github.com/olafkfreund/CFactory/pull/460)).

## Stalled work stays on the board

Stalled or stuck work items are kept on the board with their history and their
"needs you" count, instead of being deleted by the progress poller. A dead item
is removed, by default after 24 hours without an update (`CFACTORY_STALLED_PRUNE_HOURS`, configurable). A
running stage is never deleted by that sweep, however old. It is flagged stalled
after an idle deadline instead (default 900 s, configurable with
`CFACTORY_STALL_DEADLINE_SECONDS`)
([#483](https://github.com/olafkfreund/CFactory/pull/483), shipped by
[#485](https://github.com/olafkfreund/CFactory/pull/485)). One limit is recorded
in the code: if the upstream still lists a removed dead task, the next poll
recreates the row with a fresh clock
([`store.py`](https://github.com/olafkfreund/CFactory/blob/main/apps/backend/cfactory/store.py#L633-L673)).

## Cards reach the producers, and the three services join up

- The card's full body (goal, what to build, acceptance criteria, out of scope)
  is sent to the planner, coder and verifier, not only the acceptance criteria
  ([#435](https://github.com/olafkfreund/CFactory/pull/435), shipped by
  [#436](https://github.com/olafkfreund/CFactory/pull/436)).
- The AIFactory dispatch payload carries the issue number, so completion usage
  correlates to the card instead of showing `$0.00`
  ([#427](https://github.com/olafkfreund/CFactory/pull/427), shipped by
  [#428](https://github.com/olafkfreund/CFactory/pull/428)).
- The correlation key is repo-scoped, and CFactory accepts both old and new
  formats ([#487](https://github.com/olafkfreund/CFactory/pull/487), shipped by
  [#488](https://github.com/olafkfreund/CFactory/pull/488)). Bare-number
  fallbacks were then removed, which is a behaviour change: old rows stay
  readable, but a bare key is only matched exactly
  ([#506](https://github.com/olafkfreund/CFactory/pull/506), shipped by
  [#507](https://github.com/olafkfreund/CFactory/pull/507)).
- Cards join AIFactory's project-scoped `af-<project_id>:<spec_id>` key.
  With no task id no key is set, and a derived key over 128 characters is not
  set ([#509](https://github.com/olafkfreund/CFactory/pull/509)). Its PR body
  reports no `af-` rows in production, so it needed no migration.

## Copilot and approval actions that do what they say

- Reject review sends a valid AIFactory `apply-correction` body
  ([#499](https://github.com/olafkfreund/CFactory/pull/499), shipped by
  [#503](https://github.com/olafkfreund/CFactory/pull/503)).
- Propose answers 409 with a reason when there is nothing to act on, and the
  Copilot panel's Propose buttons use real action kinds. They had sent kinds
  that do not exist and silently got a 400
  ([#469](https://github.com/olafkfreund/CFactory/pull/469), shipped by
  [#470](https://github.com/olafkfreund/CFactory/pull/470)).
- The MCP endpoint accepts JSON-RPC notifications, which fixes an MCP handshake
  that failed for Claude Code
  ([#440](https://github.com/olafkfreund/CFactory/pull/440), shipped by
  [#441](https://github.com/olafkfreund/CFactory/pull/441)).
- `GET /api/audit` supports `after_id` and `limit` paging
  ([#491](https://github.com/olafkfreund/CFactory/pull/491), shipped by
  [#493](https://github.com/olafkfreund/CFactory/pull/493)).

## Access control, staged and mostly not enforcing yet

- With no `CFACTORY_API_KEYS`, `/api/*` and `/connect/*` return 401 unless
  `CFACTORY_API_DEV_OPEN=true` (default off). The sync PR says the live
  deployment sets keys, so this changed nothing in production
  ([#500](https://github.com/olafkfreund/CFactory/pull/500), shipped by
  [#503](https://github.com/olafkfreund/CFactory/pull/503)).
- `/api/events*` checks for a write-scoped key in **warn mode** by default
  (`CFACTORY_EVENTS_AUTH_MODE`). `warn` accepts, logs and counts, and `enforce`
  rejects. The live deployment runs in warn
  ([#502](https://github.com/olafkfreund/CFactory/pull/502), shipped by
  [#503](https://github.com/olafkfreund/CFactory/pull/503)).
- Use of the legacy shared MCP secret is counted and logged
  (`CFACTORY_MCP_LEGACY_SECRET_MODE`, default `allow`). This gives visibility,
  not enforcement
  ([#504](https://github.com/olafkfreund/CFactory/pull/504), shipped by
  [#505](https://github.com/olafkfreund/CFactory/pull/505)).
- A git connection's `base_url` is checked against SSRF before a credential is
  sent to it
  ([#413](https://github.com/olafkfreund/CFactory/pull/413)).

## CI and supply-chain gates

- Secret scanning with gitleaks on the PR diff, nightly and over full history
  ([#384](https://github.com/olafkfreund/CFactory/pull/384),
  [#394](https://github.com/olafkfreund/CFactory/pull/394),
  [#398](https://github.com/olafkfreund/CFactory/pull/398)).
- A whole-repo blocking security-sink gate, and a banned-constructs gate wired
  in as blocking
  ([#371](https://github.com/olafkfreund/CFactory/pull/371),
  [#392](https://github.com/olafkfreund/CFactory/pull/392)).
- CodeQL widened to the security-and-quality suite, and a weekly CodeQL ratchet
  with a zero baseline for critical, high, error and medium alerts
  ([#368](https://github.com/olafkfreund/CFactory/pull/368),
  [#494](https://github.com/olafkfreund/CFactory/pull/494), shipped by
  [#497](https://github.com/olafkfreund/CFactory/pull/497)).
- An API contract drift gate replays the backend's real responses through the
  frontend's zod schemas and diffs a golden file. It runs in CI only
  ([#445](https://github.com/olafkfreund/CFactory/pull/445), shipped by
  [#446](https://github.com/olafkfreund/CFactory/pull/446)).

## Security hygiene and dependencies

- CodeQL log-injection, stack-trace-exposure, empty-except and dead-code alerts
  were closed ([#372](https://github.com/olafkfreund/CFactory/pull/372),
  [#378](https://github.com/olafkfreund/CFactory/pull/378),
  [#380](https://github.com/olafkfreund/CFactory/pull/380),
  [#409](https://github.com/olafkfreund/CFactory/pull/409)).
- Error responses no longer name internal endpoints or return raw exception text
  ([#383](https://github.com/olafkfreund/CFactory/pull/383),
  [#391](https://github.com/olafkfreund/CFactory/pull/391),
  [#402](https://github.com/olafkfreund/CFactory/pull/402)).
- Shared code was re-vendored from the hub, and one change closes a WIQL injection
  in the vendored factory-github
  ([#385](https://github.com/olafkfreund/CFactory/pull/385),
  [#387](https://github.com/olafkfreund/CFactory/pull/387),
  [#415](https://github.com/olafkfreund/CFactory/pull/415),
  [#474](https://github.com/olafkfreund/CFactory/pull/474)).
- Frontend zod moved from 3 to 4.6.6 for CVE-2023-54404
  ([#496](https://github.com/olafkfreund/CFactory/pull/496)), and Debian security
  updates were applied to the backend base image
  ([#400](https://github.com/olafkfreund/CFactory/pull/400)).

## In progress

Merged to `dev` on 2026-11-14 and not on `main`, so not deployed:

- A model-gateway route for the copilot, used only when
  `CFACTORY_MODEL_GATEWAY_URL` is set, with no fallback to the provider on a
  gateway error
  ([#513](https://github.com/olafkfreund/CFactory/pull/513)).
- Per-factory scoped tokens (`CFACTORY_PFACTORY_TOKEN`,
  `CFACTORY_TFACTORY_TOKEN`), step 1 of a four-PR set. The cut-over needs all
  four deployed
  ([#510](https://github.com/olafkfreund/CFactory/pull/510)).

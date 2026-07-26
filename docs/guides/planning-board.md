---
layout: default
title: Planning board
permalink: /guides/planning-board/
---

# The agent-native planning board

CFactory has always been a *read* surface over work already in flight: something
lands in PFactory, AIFactory or TFactory, and the cockpit threads it. RFC-0019
adds the half in front of that — **a planning board where the work is written
down before it exists**, by a human in the cockpit or by an agent over MCP, with
the same operations available to both.

The point is not "CFactory got a Kanban". The point is that a card in `ready`
with a difficulty tier **enters the factory by itself**, comes back joined to a
work item, and then has its status written by the pipeline rather than by
whoever remembers to drag it.

This guide covers everything RFC-0019 shipped. Every feature and every setting
is stated as a user story first, then all its options, then what happens when it
is unset or wrong.

Related reading: the [Environment reference](../dev/environment-reference.md)
for the raw variable table, [Multi-Tenant Mode](multi-tenant.md) for how card
partitioning works, and [Connecting editors & external clients](token-gated-api.md)
for how an external client gets a credential in the first place. For a
newcomer's first run, start with the
[Plan a card, watch it build](plan-a-card-walkthrough.md) walkthrough.

---

## Contents

- [Cards: the human-owned half of the control plane](#cards-the-human-owned-half-of-the-control-plane)
- [Every card field, and what to put in it](#every-card-field-and-what-to-put-in-it)
- [The two board views (and the third one that is not this)](#the-two-board-views-and-the-third-one-that-is-not-this)
- [The REST surface](#the-rest-surface)
- [MCP scopes: who is allowed to touch the board](#mcp-scopes-who-is-allowed-to-touch-the-board)
- [The MCP board tools](#the-mcp-board-tools)
- [Intake: how a card becomes a build](#intake-how-a-card-becomes-a-build)
- [Git integration: the settings panel](#git-integration-the-settings-panel)
- [Status write-back: the board is a live view](#status-write-back-the-board-is-a-live-view)
- [Discovery: how an agent finds all this before it has a token](#discovery-how-an-agent-finds-all-this-before-it-has-a-token)
- [Parity: the rule that keeps the two surfaces honest](#parity-the-rule-that-keeps-the-two-surfaces-honest)
- [Failure modes at a glance](#failure-modes-at-a-glance)

---

## Cards: the human-owned half of the control plane

> **As a human planner**, I want to write down work that does not exist yet —
> a title, what "done" means, how hard it is — **so that** the backlog lives
> next to the pipeline that will build it instead of in a separate tracker I
> have to reconcile by hand.

> **As an operator deploying the fleet**, I want that backlog to survive every
> upstream hiccup, **so that** a factory going dark for ten minutes never eats
> planning data a person typed.

Cards live in their **own table**, deliberately — not in `work_items`. This is
the single most important structural fact about the feature, and it is not a
style preference. `store.py` owns `reconcile_snapshot`, `prune_duplicate_stages`,
`prune_stuck` and `prune_stalled`: machinery that blanks stages and **DELETEs
work-item rows** whenever upstream polling says the task is gone. That is
correct for a mirror of upstream state and fatal for human-authored planning
data. So none of it can reach a card.

The join between the two halves is `correlation_key`:

```
cards.correlation_key  -->  work_items.correlation_key  -->  RFC-0001 timeline
        (NULL while the card is only planned)
```

`NULL` means "planned, never dispatched". Non-`NULL` means "this card is in the
factory, and here is where to watch it". That one column is also the idempotency
guard — see [Intake](#intake-how-a-card-becomes-a-build).

Source: `apps/backend/cfactory/cards.py`.

---

## Every card field, and what to put in it

> **As a human planner**, I want each field to mean exactly one thing, **so
> that** an agent reading my backlog draws the same conclusions I would.

| Field | Type | Default | Unset / wrong behaviour | Recommended |
|---|---|---|---|---|
| `card_key` | string, max 128 | auto-assigned `FCT-<n>` | Omit it and the store assigns the next `FCT-<n>` **for this tenant** (highest existing numeric suffix + 1). Supply one that is already taken and you get a **loud 409** (`card already exists`), never a silent overwrite. Immutable after creation — `PATCH` ignores it. | Omit it. Let CFactory number the board. Supply one only when mirroring an id from an external tracker. |
| `tenant_id` | string, max 64 | `default` | Never set by the caller — it is stamped from the resolved tenant. In single-tenant mode (the default) it is always `default`. See [Multi-Tenant Mode](multi-tenant.md). | Leave it to the server. |
| `title` | string, 1–512 | **required** | Empty or missing is a **loud 422** from Pydantic validation. | One line of intent, the way you would title a GitHub issue. It becomes the `# heading` of the brief sent upstream. |
| `description` | string, nullable | `null` | Free-form markdown body (RFC-0020 section 3.6). Where an imported issue's **body** lands. **Mirrored**: the host owns it exactly as it owns the title, so a local edit to a card that tracks an issue is overwritten on the next sync. | Use it for context and links. Do not put the acceptance criteria here and expect them to be verified — they are a separate, structured field, and import deliberately never parses one into the other. |
| `acceptance_criteria` | list of strings | `[]` | Empty is legal and common for a rough backlog entry. It becomes an `## Acceptance Criteria` bullet list in the dispatched brief — **an empty list means the factory gets a title and nothing else to build against**. Silent, and it will show up as a vague build. | Fill it in before you set a tier. This is the only field that tells the factory what "done" means. |
| `status` | `backlog` \| `ready` \| `in_progress` \| `blocked` \| `done` | `backlog` | Anything outside the five is a **loud 422**. There is no other status space. `ready` **with a tier** is the intake trigger. | `backlog` on create. Promote to `ready` only when the acceptance criteria are real. |
| `priority` | integer | `0` | **Lower sorts first** — 0 is the top of the backlog, and negatives are legal if you want something above everything. No validation, no range: any int is accepted. Every card at the default `0` means the board falls back to oldest-first. | Leave `0` until ordering matters, then use gaps of 10 (`10`, `20`, `30`) so you can insert between them without renumbering. |
| `tier` | `low` \| `medium` \| `hard` \| null | `null` | `null` is a legitimate board state: a card queued for triage. **A `ready` card with no tier is never dispatched** and sits there quietly — this is the one deliberate silent no-op in the feature, because it is a real planning state, not an error. Anything outside the three is a loud 422. | Set it in the same edit that moves the card to `ready`, never before. See the tier table under [Intake](#intake-how-a-card-becomes-a-build). |
| `assignee` | string, max 128, nullable | `null` | Free text — no user directory, no validation. A typo is invisible and just makes the assignee filter miss. | A human handle (`olaf`) or a factory runtime (`aifactory`) so the filter can separate "a person is on this" from "the factory has it". |
| `milestone` | string, max 128, nullable | `null` | Free text, same as assignee: a typo silently splits a release into two groups. | A short stable release name reused verbatim across cards (`v0.3`), not a date. |
| `correlation_key` | string, max 128, nullable | `null` | Normally **set for you** by the intake dispatch. Setting it by hand on a card that was never dispatched makes the card permanently un-dispatchable (it reads as "already in the factory") — silently. Clearing it on a live card makes a second dispatch possible, which is a duplicate build. | Never set or clear it by hand. Treat it as server-owned. |
| `created_at` / `updated_at` | timestamp | now | Server-owned; `updated_at` refreshes on every applied change. | Read-only. |

Two more rules that are easy to trip over:

- **`PATCH` is partial.** Only fields actually present in the request body are
  applied (the route uses `exclude_unset`). Omit a field and it is untouched;
  send an explicit `null` and the nullable field is **cleared**. Those are two
  different requests and they do two different things.
- **There is no separate move or reprioritise endpoint.** A move is
  `PATCH {"status": ...}`. A reprioritise is `PATCH {"priority": ...}`. MCP
  splits these into named tools for legibility, but they are the same operation
  underneath.

---

## The two board views (and the third one that is not this)

> **As a human planner**, I want to see the backlog as a priority-ordered list
> when I am grooming it, and as columns when I am running the week, **so that**
> I do not have to pick one shape and live with it.

The cockpit ships two views over cards and one view over the pipeline. They look
similar and are frequently confused, so be precise:

| View | File | Axis | Data |
|---|---|---|---|
| **Backlog** | `apps/frontend-web/src/BacklogView.tsx` | One priority-ordered list | Cards |
| **Planning board** | `apps/frontend-web/src/PlanningBoard.tsx` | Kanban columns = card **status** | Cards |
| **Board** (pre-existing) | `apps/frontend-web/src/Board.tsx` | Columns = **PARR stages** (plan / code / test) | Work items |

The distinction that matters:

- **Planning board** columns are `backlog / ready / in_progress / blocked /
  done`. That is *the planning axis* — where a piece of intent sits in a
  human's process.
- **Board** columns are plan / code / test. That is *the execution axis* — how
  far a running work item has travelled through PFactory, AIFactory and
  TFactory.

A single feature appears on both, and it is supposed to: it is one card on the
planning board (`in_progress`) and one work item on the pipeline board (currently
in the code stage). They are joined by `correlation_key`. The planning board
answers "what are we doing"; the pipeline board answers "where is it right now".

Backlog and Planning board share their data hook and optimistic-mutation path
(`cards.ts`) and their card chrome (`CardParts.tsx`), so a change made in one is
the same write as the same change made in the other. Both write straight through
to the card API — there is no local-only board state to lose.

---

## The REST surface

> **As a human planner using the cockpit**, and **as an operator scripting the
> board from CI**, I want plain REST over the cards, **so that** anything I can
> do by clicking I can also do with `curl`.

All routes are on the standard API surface: `require_scope("read")` for reads,
`require_scope("write")` for mutations, tenant-scoped store, and **every mutation
appends an entry to the tamper-evident HMAC audit chain** — the same chain
`/api/actions/execute` writes to.

| Method | Path | Scope | Notes |
|---|---|---|---|
| `GET` | `/api/cards` | read | The backlog, priority ascending then oldest first. Optional filters: `status`, `milestone`, `assignee`, `tier`. Returns `{"count": n, "cards": [...]}`. |
| `POST` | `/api/cards` | write | Create. `card_key` optional. **201** on success, **409** if the tenant already holds that key. Runs the intake hook. |
| `GET` | `/api/cards/{card_key}` | read | One card. **404** if it does not exist *in your tenant scope*. |
| `PATCH` | `/api/cards/{card_key}` | write | Partial update — also how you move and reprioritise. **404** if unknown. Runs the intake hook. |
| `POST` | `/api/cards/import` | write | Import the connected repository's **existing** issues as cards (RFC-0020 section 3.6). Idempotent, incremental (`?full=true` re-reads everything), and imported cards are **never `ready`**. **200** with `ok: false` when the provider is unreachable. See [github-card-sync.md](github-card-sync.md#importing-a-repos-existing-issues-rfc-0020-section-36). |
| `DELETE` | `/api/cards/{card_key}` | write | Takes the card off the board. **404** if unknown. Returns `{"card_key": ..., "deleted": true}`. A **soft** delete since RFC-0020 section 3.6: every read hides it, the issue on the host is untouched, and the next import does not resurrect it. |

The tenant git configuration (RFC-0020 section 3.3) lives on the same surface —
`GET`/`PUT /api/tenants/{tenant}/git-config` and
`POST /api/tenants/{tenant}/git-config:verify` — and is covered in
[Git integration: the settings panel](#git-integration-the-settings-panel).

Filters are AND-ed and each is exact-match, not a search. An unknown filter value
(`?status=doing`) is a **loud 422** because the enum is validated; an unknown
`assignee` or `milestone` is a legal query that simply returns zero cards.

Source: `apps/backend/cfactory/routes_cards.py`, operations in
`apps/backend/cfactory/card_ops.py`.

---

## MCP scopes: who is allowed to touch the board

> **As an operator deploying the fleet**, I want an agent to be able to *watch*
> the pipeline without being able to *change* the backlog, **so that** handing a
> read token to a dashboard or a chat bot is not the same as handing it write
> access to planning.

RFC-0019 Phase 2a turned `POST /mcp` from a single-secret door into a scoped
one, because hanging write tools off a fail-open surface is how a backlog gets
mutated by anyone.

Two scopes exist: `read` and `write`. Every tool declares one in `TOOL_SCOPES`.
All five pipeline read tools plus `cfactory_list_cards` and `cfactory_get_card`
declare `read`; every board mutation declares `write`.

**Unregistered tools default to WRITE.** A tool added without a `TOOL_SCOPES`
entry fails closed rather than silently inheriting read access.

Credentials are checked in this precedence order:

1. **`CFACTORY_MCP_SECRET`** — the legacy full-scope bearer. A caller presenting
   it holds read *and* write, exactly as before RFC-0019. Existing production
   clients keep working with no change.
2. **`CFACTORY_API_KEYS`** — the same scoped keystore that gates `/api` and
   `/connect`, format `"<key>:read;<key2>:read,write"`. A key may call a tool
   only if it carries that tool's declared scope.
3. **Nothing configured** — **DENIED**, with a 401 that names the fix. This is a
   deliberate reversal: unconfigured used to mean open.

### Every relevant variable

| Variable | Values | Default | Unset / wrong | Recommended |
|---|---|---|---|---|
| `CFACTORY_MCP_SECRET` | any string | unset | Unset alone is fine if `CFACTORY_API_KEYS` is set. A **wrong** token presented against a configured server is a **loud 401** (`Invalid MCP token`) — compared in constant time. | Set it in hosted deploys for the existing full-scope clients; prefer scoped keys for anything new. |
| `CFACTORY_API_KEYS` | `<key>:read,write;<key2>:read` | unset | Unset means the keystore is not "configured", so `/api` and `/connect` run **OPEN** (single-user local mode) and `/mcp` falls through to rule 3. A key present but lacking the scope is a **loud 403** naming the missing scope. | In hosted: one `read` key per watcher, one `read,write` key per agent that plans. Never one shared key. |
| `CFACTORY_MCP_DEV_OPEN` | `true` \| `false` | `false` | When `false` **and** nothing else is configured, every `/mcp` call is a **loud 401** telling you to set `CFACTORY_MCP_SECRET` or `CFACTORY_API_KEYS`. **Ignored entirely** once either of those is set — you cannot use it to reopen a configured server. | `false` everywhere. Set `true` only on a laptop. Never in a hosted or shared deploy. |

Authorization applies at `tools/call`. `initialize` and `tools/list` still
require a valid credential (the whole endpoint authenticates first), but scope is
checked per tool, so a read key can enumerate every tool including the write ones
and will get a 403 when it tries to call one. That is intentional: an agent
should be able to discover what exists and learn it lacks permission, rather than
see a fictitious smaller catalogue.

Source: `apps/backend/cfactory/mcp.py`, scope primitives in `auth.py`.

---

## The MCP board tools

> **As an agent / MCP client**, I want to manage the planning backlog with the
> same operations a human has in the cockpit, **so that** I can groom, plan and
> dispatch work without a human having to click for me — and without a second,
> subtly different implementation that drifts from theirs.

These tools do not reimplement anything. They call `cfactory.card_ops` — the
same store, the same audit chain, the same intake dispatch the REST routes use.
A card created over MCP is byte-identical to one created over `POST /api/cards`,
and an agent moving a card to `ready` with a tier dispatches it into the factory
exactly as a human's PATCH would.

| Tool | Scope | Arguments | Notes |
|---|---|---|---|
| `cfactory_list_cards` | read | `status`, `milestone`, `assignee`, `tier` (all optional) | The backlog, highest priority first. Same filters as `GET /api/cards`. |
| `cfactory_get_card` | read | `card_key` (required) | Full card including `correlation_key`. |
| `cfactory_create_card` | write | `title` (required); `card_key`, `acceptance_criteria`, `status`, `priority`, `tier`, `assignee`, `milestone`, `correlation_key` | Omit `card_key` for the next `FCT-<n>`. Creating straight into `ready` with a tier dispatches immediately. |
| `cfactory_update_card` | write | `card_key` (required) + `title`, `acceptance_criteria`, `tier`, `assignee`, `milestone` | Content edits only — the schema deliberately does not expose `status` or `priority`. |
| `cfactory_move_card` | write | `card_key`, `status` (both required) | The board move. Moving to `ready` **with a tier** is the intake trigger. |
| `cfactory_reprioritise_card` | write | `card_key`, `priority` (both required) | Reordering only. |
| `cfactory_import_cards` | write | `project`, `full` (both optional) | Import the connected repository's **existing** issues as cards (RFC-0020 section 3.6). Idempotent; imported cards are **never `ready`**; pull requests are never imported; poll-based, not live. |
| `cfactory_delete_card` | write | `card_key` (required) | Takes the card off the board (a soft delete — the issue on the host is untouched and the next import does not bring it back). |

`update`, `move` and `reprioritise` are three tool names over **one** handler —
they are the same partial update, differing only in which field the agent-facing
schema exposes. Splitting them in the catalogue makes intent legible to a model
choosing a tool; splitting them in the implementation would just be three copies
of one line.

**Errors come back as data, not as protocol failures.** Where REST returns
404/409/422, MCP returns a JSON `{"error": ...}` payload in the tool result:
`no card 'FCT-9'`, `card already exists: 'FCT-3'`, or
`{"error": "invalid arguments", "details": [...]}`. An agent reads and recovers
rather than seeing an opaque JSON-RPC internal error. A genuinely unexpected
exception *is* a JSON-RPC `-32603`, and is logged server-side.

Client configuration:

```json
{
  "mcpServers": {
    "cfactory": {
      "type": "http",
      "url": "${CFACTORY_URL}/mcp",
      "headers": {"Authorization": "Bearer ${CFACTORY_MCP_TOKEN}"}
    }
  }
}
```

MCP writes are tenant-scoped from the `X-Tenant-Id` header exactly as REST
writes are, so an MCP write lands in the same partition. The audit trail records
`/mcp` as the endpoint, so you can always tell which surface a change arrived
through.

---

## Intake: how a card becomes a build

> **As a human planner**, I want promoting a card to `ready` to *actually start
> the work*, **so that** there is no second manual step where I re-type the same
> intent into a factory and hope the two stay in sync.

**The trigger is a card's state, not a verb.** Any write — create or update,
REST or MCP — that leaves a card in `status: ready` **with a tier set**
dispatches it into the factory. A card created directly into `ready` with a tier
dispatches on creation, exactly as a later promotion would.

Tier decides which door it goes through (RFC-0011 §3):

| Tier | Destination | Endpoint | Why |
|---|---|---|---|
| `low` | AIFactory | `POST /api/tasks/from-issue` | Skip-planning fast path — straight to a build. Requires the tenant's **AIFactory project id**. |
| `medium` | AIFactory | `POST /api/tasks/from-issue` | Same fast path. Requires the tenant's **AIFactory project id**. |
| `hard` | PFactory | `POST /api/plan/sessions/ingest-text` | Full decomposition before any code is written. **Needs no project id.** |
| unset | nowhere | — | Not an intake event. The card sits in `ready`, untouched. |

The card is rendered as a markdown brief both doors accept — the title as an `#`
heading, acceptance criteria as an `## Acceptance Criteria` bullet list. For the
AIFactory path the tier also travels as a `factory:<tier>` label, which is what
AIFactory's classifier reads, and `auto_continue: true` is set.

The dispatch reuses the cockpit's existing confirmed-write path verbatim (a
`PreparedAction` executed by `execute_action`), so it inherits the SSRF endpoint
guard, the upstream bearer token, and the never-raises contract.

### Idempotency

There is no new "dispatched" column. **`correlation_key` non-NULL is
"already in the factory"**, so re-promoting a live card returns
`{"dispatched": false, "ok": true, "reason": "already dispatched"}` and does
nothing else. You cannot double-build a card by clicking twice, by racing REST
against MCP, or by a retry.

### When dispatch fails

A card that could not enter the factory **must not sit in `ready` looking
dispatched**, so it is moved to `blocked`, and the audit entry carries the
upstream status code with `ok=false`. This is the loud path: the card visibly
changes column, and the trail says why. It is never a 500 on your PATCH and
never a JSON-RPC internal error — the dispatch is designed not to raise.

The response body tells you which case you are in:

```json
{"dispatched": true,  "target_service": "aifactory", "correlation_key": "task-123", "ok": true}
{"dispatched": false, "ok": true,  "reason": "already dispatched", "correlation_key": "task-123"}
{"dispatched": false, "ok": false, "status_code": 0, "reason": "no AIFactory project configured — ..."}
```

If the upstream accepts the card but returns no correlation key of any
recognised shape (`correlation_key`, `task_id`, `session_id`, `id`, `spec_id`),
CFactory falls back to the card's **own** `card_key` rather than leaving it
unjoinable.

Source: `apps/backend/cfactory/card_intake.py`.

---

## Git integration: the settings panel

> **As a human planner**, I want to say in the portal which repository my board
> syncs with, **so that** connecting a project is something I can do and check,
> rather than a redeploy I have to ask an operator for.

> **As an operator deploying the fleet**, I want that choice stored per tenant
> and audited, **so that** two tenants on one cockpit cannot file into each
> other's repositories and I can reconstruct who pointed the board where.

> **As a GitLab user**, I want the same board, **so that** "the fleet supports
> GitLab" is true of the planning surface and not only of the runners.

RFC-0020 section 3.3 turns git configuration into a **tenant-level resource**.
Before it, two process-global environment variables decided which host and which
project every tenant talked to, and one of them —
`CFACTORY_INTAKE_PROJECT_ID` — was an opaque UUID with no explanation reachable
from the portal. Now a tenant has exactly one git configuration, it lives in
**Settings > Git integration**, and it is what every part of the board reads:
opening an issue for a `ready` card, importing a repository's existing issues,
and dispatching a card into AIFactory.

Credentials are **not** part of it. The token is still the deployment's, from
the environment; RFC-0020 phases 3 and 4 own credential custody. This follows the
copilot settings precedent exactly — the provider and the model persist, the API
key never does — and it is why `credential_missing` is one of the statuses.

### Every setting, in full

| Setting | Type | Default | What it decides |
|---|---|---|---|
| **Provider** | `github` \| `gitlab` \| `azure_devops` | `github` | Which host implementation the board talks through. These three are the ones actually implemented; Bitbucket and Gitea exist in the provider protocol and are not offered, because a dropdown entry that only ever errors is a lie. |
| **Host** (`base_url`) | http(s) origin | the provider's public default (`https://api.github.com`, `https://gitlab.com`, `https://dev.azure.com`) | Where the API calls go. This is the field that makes a **self-hosted GitLab, GitHub Enterprise or Azure DevOps Server** work. |
| **Project** | provider path | unset | Where a `ready` card **opens** its issue: `owner/repo` on GitHub, `group/subgroup/project` on GitLab, `organization/project/repo` on Azure DevOps. |
| **Import from** (`intake_project`) | provider path | falls back to **Project** | Where the import **reads** existing issues from, when that differs from the project above. Leave it empty unless you genuinely have two repositories. |
| **AIFactory project id** | project id (a UUID in practice) | unset | Which AIFactory project a dispatched card is **built** in. Not a repository path — see below. |
| **Default labels** | list of strings | empty | Labels put on issues the board opens. A `factory:<tier>` label is **refused**: that label is the fleet's own intake trigger (RFC-0011), so it would build the same card a second time. |
| **Status** | derived, read-only | `unconfigured` | `unconfigured` (no project named) -> `credential_missing` (a project, but the deployment has no usable token) -> `configured` (reachable in principle, never proved) -> `verified` (proved by **Verify**). Never stored as a field: it is a function of the configuration, the credential and the last verification, and a stored copy would go stale. |

### The AIFactory project id, in full

This is the setting that was impossible to guess from its old name
(`CFACTORY_INTAKE_PROJECT_ID`), so here is the whole situation.

**The problem it solves.** AIFactory's `/api/tasks/from-issue` requires a
`project_id` — that is how AIFactory knows *which repository* to build in. A
planning card carries no project: it is a title, some acceptance criteria and a
tier. That gap has to be closed by configuration, because it is a deliberate
decision (which repo does this board's work build into?), not something a card
or an agent can supply.

**It is not a repository path.** It is AIFactory's own project id. The board
never resolves it, never validates it, and never guesses it from the git project
above them — the two live in different namespaces, and defaulting one to the
other would send a repo path where a UUID is required.

**Scope.** It affects `low` and `medium` cards, plus every explicit `code` and
`test` stage action. `hard` cards route to PFactory, which takes free text and
needs no project id, so a board that only ever plans `hard` work never needs it.

**What happens when it is unset.** The dispatch is not attempted. The card is
moved to **`blocked`** and the response carries the reason:

```
no AIFactory project configured — set one in Settings > Git integration to
dispatch a low/medium card to AIFactory
```

An explicit stage action refuses instead, with a **409** and the machine-readable
code `no_intake_project`. Both fail **LOUD**: the card visibly changes column or
the call is refused, and an `ok=false` entry lands in the audit chain. It is
never accepted-and-dropped, and it is never a startup error — a board that only
plans `hard` cards is a legitimate configuration.

**What happens when it is wrong.** A syntactically fine but non-existent project
id is *not* caught here — it is sent upstream and AIFactory rejects it. The
dispatch comes back `ok=false`, so the card still ends up `blocked`, with the
upstream status code in the audit entry. Loud, but one hop further away: check
the audit trail's `status_code`, not the card, to tell "unset" (status 0, reason
text) from "wrong" (an upstream 4xx).

The genuinely dangerous case is a **valid id pointing at the wrong project** —
nothing here can detect that, and the card will build successfully into a
repository you did not mean to touch. Which is exactly why the recommendation
below is what it is.

**Recommended value.** `5d78d4b9-35f9-4445-92c1-78f3ff60a494` (`aifactory-demo`)
— the value the hosted deployment runs, and the choice is deliberate:
`aifactory-demo` is the **sacrificial demo repository**. An autonomous build
triggered from a planning card opens PRs and writes code, so the default
destination should be a repo where an unwanted, wrong or experimental build costs
nothing. Point it at a real service's project only once you trust the board's
promotion discipline, and never at a repo whose main branch matters before then.

For a local run, either leave it empty (and plan `hard` cards, which route to
PFactory) or set it to a throwaway project id in your local AIFactory.

### What happens when the rest is unset or wrong

| Situation | What happens | Loud or quiet |
|---|---|---|
| No **project** | A `ready` card opens no issue and the sync hook stays quiet — a deployment that named no project has opted into mirroring *adopted* issues, not into filing new ones. Asking explicitly (`POST /api/cards/{key}/sync-github`) says plainly that no project is configured. An import returns `ok: false` with the same reason. | Quiet on the hook, loud when asked |
| **Project** in a format the provider cannot address | Refused on save with a **400** naming the expected shape. It never reaches the point where every card write fails with an error nobody connects back to this form. | Loud, at save time |
| **Project** valid but non-existent | Saves fine (nothing can know without asking). **Verify** turns it into a recorded failure; without verifying, the first card write records `github_sync_error` on the card and the board keeps serving. | Loud once you press Verify |
| No **credential** on the deployment | Status is `credential_missing`. Sync is off entirely: no network call on a card write, no issue opened. | Quiet by design, visible in the panel |
| **Host** not an http(s) origin | Refused on save with a **400**. It becomes an HTTP client's base URL, so it is validated where it enters rather than trusted. | Loud, at save time |
| `factory:<tier>` in **default labels** | Refused on save with a **400** explaining that it is the intake trigger. Silently dropping it would look saved and would not be. | Loud, at save time |
| Wrong **provider** for the host | The provider's own API returns a 404 or a parse failure; the reason lands on the card (`github_sync_error`) or in the verify result. Nothing 500s. | Loud, one hop away |

### Verify

**Verify** makes exactly one authenticated read of the repository
(`get_repository_info` on the provider protocol). That single call answers all
three questions at once: does the host resolve, is the credential accepted, and
can it see the project. The result is recorded, so the status becomes `verified`
or keeps the failure reason.

Saving **clears** a previous verification, because it proved a configuration the
current one no longer is. The panel disables **Verify** while there are unsaved
edits for the same reason.

An unreachable host is `ok: false` with the reason, never an error page.

### The surface

| Method | Path | Scope | MCP twin |
|---|---|---|---|
| `GET` | `/api/tenants/{tenant}/git-config` | read | `cfactory_get_git_config` |
| `PUT` | `/api/tenants/{tenant}/git-config` | write | `cfactory_set_git_config` |
| `POST` | `/api/tenants/{tenant}/git-config:verify` | write | `cfactory_verify_git_config` |

`PUT` is a **full replacement**: an omitted optional field is cleared. Every
mutation appends to the same tamper-evident HMAC audit chain the card mutations
use, keyed on `tenant:<id>`.

**The tenant in the path is checked, not trusted.** It is a URL segment a caller
chooses; the tenant a caller may actually touch comes from the resolved request
identity (`X-Tenant-Id`, injected by oauth2-proxy from the Keycloak claim, never
from the browser). Naming another tenant is a **403**. In single-tenant mode
every request resolves to `default`, so naming anything else is a 403 there too.
The MCP tools take no tenant argument at all — an agent operates on its own
partition or on nothing.

Until phase 3 lands, a **multi-tenant deployment shares the operator's
environment credential**: each tenant chooses its own project, and all of them
reach it with the same token. That is safe for a single-tenant deployment and
explicitly not a tenant isolation boundary for credentials. Phase 3 (#364) moves
credentials into the tenant; phase 4 adds OAuth.

### Migrating from the environment variables

`CFACTORY_INTAKE_PROJECT_ID`, `CFACTORY_GITHUB_REPO`, `CFACTORY_GIT_PROVIDER`
and `CFACTORY_GIT_PROVIDER_URL` are **retired as configuration** and survive one
release as a **seed**. On first boot, a tenant with no stored configuration
materialises one from whichever of them are set; from then on the stored
configuration is authoritative and editable, and a restart never overwrites an
edit. A tenant that has never been seeded and has no stored row still resolves
against the environment, so nothing breaks in the meantime.

**Existing single-tenant deployments need no operator action.** Their values
appear in the panel on the next boot, and the panel shows `source: env` until
the first save.

The variables are removed in the release after this one. Set the values in the
panel now; do not add them to a new deployment.

---

## Status write-back: the board is a live view

> **As a human planner**, I want the card to move on its own as the factory
> works, **so that** the board is the truth rather than a stale copy someone has
> to maintain alongside the real one.

Once a card carries a `correlation_key`, the **same completion-event stream**
that threads the work-item timeline also writes the card's status. Every
completion-event ingress, after updating the work item, looks up the card joined
to that correlation key and maps the live PARR state onto the card's five
columns:

| Live PARR state | Card status |
|---|---|
| Any stage failed or stuck | `blocked` |
| TFactory stage done | `done` |
| AIFactory stage done | `done` |
| **PFactory stage done** | **`in_progress`** |
| Anything else in flight | `in_progress` |
| No stage state at all | unchanged |

**A finished plan is not a verdict.** That is why a done PFactory stage maps to
`in_progress` and not `done`: PFactory finishing means the plan is ready and the
*build has not started*. Marking that card `done` would be the board lying about
completed work — the most expensive kind of wrong a planning tool can be. Only a
verdict from further down the pipeline closes a card.

Stages are checked furthest-along first (TFactory, then AIFactory, then
PFactory), which is the same ordering the review-target logic uses to find the
stage actually in flight.

The write-back is a **no-op** when no card is joined to that correlation key —
which is the ordinary case for work that came in as a GitHub issue rather than
from the board — and a no-op when the mapped status equals the current one. It
never fights a human: it writes the mapped status, so if you manually move a
live card the next event puts it back where the pipeline says it is. Move a card
you want to hold by hand *out of* the factory (or accept that the pipeline owns
it now).

---

## Discovery: how an agent finds all this before it has a token

> **As an agent / MCP client**, I want to find out what a service can do
> *before* I authenticate, **so that** I can decide whether it is worth asking
> for a credential — the same way I would read a README before cloning.

Two endpoints, both readable **without authentication**:

| Path | What it returns |
|---|---|
| `/.well-known/agent-skills/index.json` | CFactory's own manifest: service name, version, description, the full skill list (derived from the live MCP tool catalogue), the MCP endpoint, the OpenAPI path. |
| `/.well-known/agent-skills/fleet.json` | The whole Factory: CFactory's manifest plus PFactory's, AIFactory's and TFactory's, folded in from each origin. |

Unauthenticated is safe here **by construction**, not by exception: the API-key
middleware guards `/api/*` and `/connect/*` only, so these paths are outside it,
and there is a test that asserts this holds under an enforced keystore. The
payload is public metadata only — service identity, version, capability
names and descriptions already published by the tool catalogue, and relative
paths. No tokens, no internal hostnames, no work-item or card data.

The skills list is **derived from `MCP_TOOLS`** rather than restated, so the
manifest cannot drift from the server. Add a board tool and it appears here
automatically.

The fleet aggregate is best-effort by design:

- Sibling manifests are cached for **60 seconds** (they change only on deploy),
  and the response carries `Cache-Control: public, max-age=60`.
- Each sibling fetch has a **3 second timeout** — one slow service must not hold
  the aggregate open.
- A sibling fetched successfully before but down now keeps its **last-good body**
  in `services[]`, flagged `reachable: false`.
- A sibling **never** successfully reached appears in a separate `unavailable[]`
  array with a coarse reason (`unreachable` or `manifest incomplete`) — announced
  rather than omitted, so an agent learns the service exists and is temporarily
  undiscoverable instead of concluding the fleet is smaller than it is. Nothing
  is invented: no fake version, endpoint or skill list.
- CFactory's own entry is always present, so `services[]` is never empty even
  with all three siblings down.
- Only an allow-listed set of fields is copied from each sibling, so a sibling
  that puts something unexpected in its manifest cannot leak it through this
  public endpoint.

Sibling origins come from `CFACTORY_PFACTORY_API_URL`,
`CFACTORY_AIFACTORY_API_URL` and `CFACTORY_TFACTORY_API_URL` — never hardcoded,
and runtime-editable from the Services view. Point those at localhost and the
fleet aggregate describes your laptop; point them at the hosted origins and it
describes production.

`Access-Control-Allow-Origin: *` is set on the fleet aggregate, because a
browser-based agent is a first-class consumer of a public manifest and the app's
normal CORS policy only allows the cockpit's own origin.

Source: `apps/backend/cfactory/routes_well_known.py`.

---

## Parity: the rule that keeps the two surfaces honest

> **As an agent / MCP client**, I want a guarantee that anything a human can do
> on this board, I can do too, **so that** I never hit a capability cliff where
> the task requires a browser.

RFC-0019 §3.3's design law is: **every board action a human can take has an
identical REST + MCP equivalent.** `tests/test_board_parity.py` is the CI check
that keeps that true rather than aspirational.

It works at the level of board **operations**, not names — REST's one
`PATCH /api/cards/{card_key}` backs three MCP tools, and a 1:1 name match would
be the wrong test. Both surfaces are **enumerated live** (the app's own generated
OpenAPI document; the real `MCP_TOOLS` catalogue) rather than hand-copied, and
compared against the operation map in both directions. Three ways to fail:

1. A live REST route under `/api/cards` that no operation claims — you added a
   route and forgot the tool.
2. A live MCP tool with `card` in its name that no operation claims — you added
   a tool and forgot the route.
3. A map entry whose route or tool no longer exists — the map went stale.

A fourth test asserts scopes follow from the HTTP method: every mutating
operation declares `write`, every read declares `read`. A read-scoped key must
be able to enumerate the backlog and unable to change it, **over either
transport**.

Reading the OpenAPI document rather than the router objects is deliberate: a
route that is served but undocumented is not parity either, because that is the
contract an agent actually discovers.

---

## Failure modes at a glance

| What you did | What happens | Loud or silent? |
|---|---|---|
| `POST` a `card_key` that already exists in your tenant | 409 / `card already exists` | Loud |
| `PATCH` or `GET` an unknown card key | 404 | Loud |
| Send an invalid `status` or `tier` | 422 from validation | Loud |
| Point a second card at an issue another card already tracks | 409 / `another card already tracks issue ...` — one issue, one card, enforced by a unique index | Loud |
| Import from a repo with more issues than `CFACTORY_IMPORT_MAX` | `truncated: true` in the result and in the board's import summary | Loud |
| Expect an issue filed a moment ago to be on the board | It appears on the next import/poll, not instantly — import is **not live** (no webhook receiver) | Documented, and `last_synced_at` shows the staleness |
| Promote to `ready` with **no tier** | Nothing. Card sits in `ready`. | **Silent — and intended.** It is a real triage state. |
| Promote a `low`/`medium` card with **no AIFactory project id** | Card moves to `blocked`, reason points at Settings > Git integration, `ok=false` audit entry | Loud |
| Read or write `/api/tenants/<someone else>/git-config` | 403 — the tenant in the path is checked against the tenant the request resolved to | Loud |
| Save a `factory:<tier>` default label | 400 — it is the fleet's intake trigger and would build the card twice | Loud, at save time |
| Restart after editing the git config in the panel | Nothing. The seed runs once; a stored configuration is never overwritten by the environment | Silent — and the point |
| Promote with a wrong-but-valid-looking project id | Upstream rejects; card moves to `blocked` with the upstream status in the audit entry | Loud (one hop away) |
| Promote with a valid id for the **wrong project** | Builds successfully into the wrong repo | **Silent.** Nothing can detect this — see the recommendation above. |
| Promote an already-dispatched card again | `{"dispatched": false, "ok": true, "reason": "already dispatched"}` | Loud in the response, no side effect |
| Set `correlation_key` by hand on an undispatched card | The card becomes permanently un-dispatchable | **Silent.** Do not do this. |
| Call `/mcp` with nothing configured and `CFACTORY_MCP_DEV_OPEN=false` | 401 naming the two variables that fix it | Loud |
| Call a write tool with a `read`-scoped key | 403 naming the missing scope | Loud |
| Add an MCP tool without a `TOOL_SCOPES` entry | It requires `write` (fails closed) — and `test_board_parity.py` fails if it is a board tool | Loud, in CI |
| Add a REST card route without an MCP twin | `test_board_parity.py` fails | Loud, in CI |
| Upstream factory unreachable when the fleet manifest is fetched | Last-good body with `reachable: false`, or an `unavailable[]` entry | Loud in the payload, never a 5xx |

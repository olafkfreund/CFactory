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
- [Many repositories, many providers: connections](#many-repositories-many-providers-connections)
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
| `repository_id` | integer, nullable | `null` | Which of the tenant's configured repositories this card is for (RFC-0020 section 3.3 phase 8). `null` means **the tenant's default repository** — which is what every card created before this phase means, and what a card whose repository has since been deleted falls back to. It decides which host the card's issue is opened on, which credential is used, and which AIFactory project its build lands in. A card whose `issue_ref` names a configured repository resolves to that one even when this is `null`. See [Many repositories, many providers](#many-repositories-many-providers-connections). | Leave it unset on a single-repository board. Set it in the same edit that picks the repo on a board with several, rather than relying on the default. |
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
from the portal. Now git configuration lives in **Settings > Git integration**, and it is what
every part of the board reads: opening an issue for a `ready` card, importing a
repository's existing issues, and dispatching a card into AIFactory.

A tenant used to have exactly ONE such configuration — one provider against one
repository. RFC-0020 phase 8 replaced that with **connections and repositories**:
many hosts per tenant, many repositories per host, and one repository that a card
naming none falls back to. Read
[Many repositories, many providers](#many-repositories-many-providers-connections)
for that model; this section describes the single-configuration view, which is
still served and still works, and now reads and writes the tenant's **default
repository**.

The credential is part of it since RFC-0020 section 3.4, as its own write-only
resource: encrypted at rest, never returned by anything, and per tenant. See
[The credential](#the-credential) below. A deployment that has not stored one
still falls back to the environment credential it has always used.

### Every setting, in full

| Setting | Type | Default | What it decides |
|---|---|---|---|
| **Provider** | `github` \| `gitlab` \| `azure_devops` | `github` | Which host implementation the board talks through. These three are the ones actually implemented; Bitbucket and Gitea exist in the provider protocol and are not offered, because a dropdown entry that only ever errors is a lie. |
| **Host** (`base_url`) | http(s) origin | the provider's public default (`https://api.github.com`, `https://gitlab.com`, `https://dev.azure.com`) | Where the API calls go. This is the field that makes a **self-hosted GitLab, GitHub Enterprise or Azure DevOps Server** work. |
| **Project** | provider path | unset | Where a `ready` card **opens** its issue: `owner/repo` on GitHub, `group/subgroup/project` on GitLab, `organization/project/repo` on Azure DevOps. |
| **Import from** (`intake_project`) | provider path | falls back to **Project** | Where the import **reads** existing issues from, when that differs from the project above. Leave it empty unless you genuinely have two repositories. |
| **AIFactory project id** | project id (a UUID in practice) | unset | Which AIFactory project a dispatched card is **built** in. Not a repository path — see below. |
| **Default labels** | list of strings | empty | Labels put on issues the board opens. A `factory:<tier>` label is **refused**: that label is the fleet's own intake trigger (RFC-0011), so it would build the same card a second time. |
| **Credential** | write-only string | unset (falls back to the deployment's) | What the board authenticates to the host with. Stored encrypted, per tenant, and never displayed again. See [The credential](#the-credential). |
| **Status** | derived, read-only | `unconfigured` | `unconfigured` (no project named) -> `credential_missing` (a project, but no usable credential — absent, undecryptable, or refused by the host on the last verify) -> `configured` (reachable in principle, never proved) -> `verified` (proved by **Verify**). Never stored as a field: it is a function of the configuration, the credential and the last verification, and a stored copy would go stale. |

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

### The credential

> **As a human planner**, I want to connect my own repository from the portal,
> **so that** my board actually reaches it without an operator putting my token
> into a deployment's environment.

> **As an operator running one cockpit for several tenants**, I want each
> tenant's credential stored separately and encrypted, **so that** tenant A's
> board cannot file issues into tenant B's repository and a database dump is not
> a list of everybody's tokens.

> **As the person who has to answer for it**, I want every use of a credential in
> the audit trail and every credential rotatable, **so that** "who used this, and
> when" and "we have rotated the key" are questions with answers.

RFC-0020 section 3.4. Before it, one environment variable held one credential for
the whole deployment: every tenant reached its chosen project with the operator's
token, which is safe for a single-tenant deployment and is not a boundary of any
kind for several. Now a tenant stores its own, in **Settings > Git integration**,
and the board uses it for that tenant and no other.

**It is write-only.** You paste it once. No endpoint returns it, no MCP tool
returns it, the panel never renders it, and it is not in any log line, error
message or audit entry. The only thing any read ever says is whether one exists,
when it was stored, and which encryption key wraps it. Changing it means storing
a new one; there is no "show".

#### Every option, and its default

| Option | Where | Default | What it decides |
|---|---|---|---|
| **Credential** | the panel, `PUT /api/tenants/{tenant}/git-credential`, or `cfactory_set_git_credential` | unset | What the board authenticates with. Any credential the provider issues: a GitHub PAT, a GitLab personal or group access token, an Azure DevOps PAT. Encrypted before it is written. |
| **Remove** | the panel, `DELETE .../git-credential`, or `cfactory_delete_git_credential` | — | Revokes it from this board. Idempotent: removing one that is not there is a 200, not an error. Removing it at the *provider* is a separate action, and the one that actually matters. |
| `CFACTORY_CREDENTIAL_KEY` | deployment environment | unset | The key that encrypts every stored credential. Full detail, including how to generate one: [the environment reference](../dev/environment-reference.md). |
| `CFACTORY_GIT_PROVIDER_TOKEN` | deployment environment | unset | The **fallback**, used only by a tenant that has stored nothing of its own. Unchanged behaviour for every existing deployment. |

**Where the credential goes, and when.** It is decrypted at exactly one moment:
when a provider is built for one call — opening an issue, importing a backlog,
running **Verify**. It is not held in a cache, a module global, or on any
long-lived object, and resolving a configuration for the panel does not decrypt
anything. Every one of those decryptions appends an entry to the same
tamper-evident audit chain the card mutations use (`read_git_credential`, keyed
on `tenant:<id>`), including the failed ones — "the credential could not be read
at 14:02" is exactly the entry needed after a key rotation goes wrong.

**How it is encrypted.** Envelope encryption. Each stored credential gets its own
random 256-bit data key and is sealed with AES-256-GCM; that data key is itself
sealed with the deployment's `CFACTORY_CREDENTIAL_KEY`, and the row records which
key did the wrapping. The tenant id is bound into both layers as associated data,
so a record moved between tenants does not decrypt at all — the isolation is
cryptographic and not only a `WHERE` clause. Rotating the key re-wraps the data
key and never decrypts the credential.

#### What happens when it is unset or wrong

| Situation | What happens | Loud or quiet |
|---|---|---|
| No credential for this tenant, and none in the environment | Status is `credential_missing`. Sync is off entirely: no network call on a card write, no issue opened. **The board keeps serving** — cards, columns, imports and the panel all answer normally. | Quiet on the hook, loud in the panel and when asked |
| No `CFACTORY_CREDENTIAL_KEY` on the deployment | Storing a credential is **refused** with a 503 naming the variable. Nothing is written, and nothing is written in the clear. | Loud, at the moment you press Store |
| `CFACTORY_CREDENTIAL_KEY` set to something that is not a key (a passphrase, a truncated value, bad base64) | Refused with an error saying what is wrong and how to generate a real one. It is never hashed or stretched into something key-shaped — that turns a typo into a weaker key nobody notices. | Loud |
| The key changes and the old one is no longer listed | Stored credentials cannot be decrypted. They read as `credential_missing`, the board keeps serving, and the audit chain records each failed read. Nothing returns garbage: AES-GCM authenticates, so a wrong key fails rather than producing a plausible string. | Loud in the panel, recoverable by restoring the old key |
| The key is **lost entirely** | Every stored credential is permanently unreadable. There is no recovery — store new ones. | Loud, unrecoverable |
| The credential is valid but the host refuses it (401/403 on **Verify**) | Status becomes `credential_missing`, not a green `configured`: a token the host will not accept is, from the board's point of view, a token it does not have. Storing a new one clears the rejection. | Loud once you press Verify |
| The credential is valid but points at the wrong account | Nothing here can detect that. **Verify** succeeds and the board files issues as that identity. | Silent — which is why the recommendation below is what it is |
| A tenant stored a credential that currently cannot be read | It gets **nothing**. It does not silently fall back to the deployment's environment credential — that fallback would hand one tenant the operator's token the moment its own record became unreadable. | Quiet by design, visible as `credential_missing` |

#### Recommended

**Store a per-tenant credential, and give it the narrowest scope that works.**
For GitHub that is a fine-grained PAT limited to the one repository, with
issues: read and write and nothing else; for GitLab, a **group or project access
token** rather than a personal one, at the `api` scope, so the integration does
not belong to a person who might leave. Azure DevOps: a PAT scoped to Work Items
(read and write) on one project.

Do not use a classic GitHub PAT with `repo`: it grants push access to every
repository you can see, and this board needs to open issues in one.

**Single-tenant, local, or a demo:** leaving `CFACTORY_GIT_PROVIDER_TOKEN` in the
environment is still supported and still correct. Store a per-tenant credential
when there is more than one tenant, or when you want the audit trail to say which
tenant's credential was used.

**Set `CFACTORY_CREDENTIAL_KEY` before you need it.** A deployment without one
cannot store credentials at all, and the failure arrives at the least convenient
moment — when somebody is trying to connect their repository.

### What happens when the rest is unset or wrong

| Situation | What happens | Loud or quiet |
|---|---|---|
| No **project** | A `ready` card opens no issue and the sync hook stays quiet — a deployment that named no project has opted into mirroring *adopted* issues, not into filing new ones. Asking explicitly (`POST /api/cards/{key}/sync-github`) says plainly that no project is configured. An import returns `ok: false` with the same reason. | Quiet on the hook, loud when asked |
| **Project** in a format the provider cannot address | Refused on save with a **400** naming the expected shape. It never reaches the point where every card write fails with an error nobody connects back to this form. | Loud, at save time |
| **Project** valid but non-existent | Saves fine (nothing can know without asking). **Verify** turns it into a recorded failure; without verifying, the first card write records `github_sync_error` on the card and the board keeps serving. | Loud once you press Verify |
| No **credential** for this tenant | Status is `credential_missing`. Sync is off entirely: no network call on a card write, no issue opened, and the board keeps serving every read. Full table above: [The credential](#the-credential). | Quiet by design, visible in the panel |
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
| `PUT` | `/api/tenants/{tenant}/git-credential` | write | `cfactory_set_git_credential` |
| `DELETE` | `/api/tenants/{tenant}/git-credential` | write | `cfactory_delete_git_credential` |

The credential has **no read row**, and that is the point: there is nothing to
pair a read tool with. Both its operations are `write` scope, so a read-scoped
key can enumerate the backlog and inspect the configuration and can neither store
nor remove a credential.

`PUT` on the configuration is a **full replacement**: an omitted optional field is cleared. Every
mutation appends to the same tamper-evident HMAC audit chain the card mutations
use, keyed on `tenant:<id>`.

**Since RFC-0020 phase 8 these five are a view onto the tenant's default
repository**, not a separate store. `GET` reads it; `PUT` writes it and the
connection it lives on, editing that connection in place rather than adding one
(so choosing a different host never strands the credential); `:verify` verifies
that connection; the credential endpoints target it. One exception is worth
knowing: clearing **Project** on this form clears the tenant's *default* and
leaves every repository intact — it does not delete anything a human configured.
An AIFactory project id belongs to a repository, so a configuration with no
project has nowhere to keep one; name a project, or use the per-repository
endpoints.

**The tenant in the path is checked, not trusted.** It is a URL segment a caller
chooses; the tenant a caller may actually touch comes from the resolved request
identity (`X-Tenant-Id`, injected by oauth2-proxy from the Keycloak claim, never
from the browser). Naming another tenant is a **403**. In single-tenant mode
every request resolves to `default`, so naming anything else is a 403 there too.
The MCP tools take no tenant argument at all — an agent operates on its own
partition or on nothing.

A tenant that has **not** stored a credential still shares the deployment's
environment one: each such tenant chooses its own project, and all of them reach
it with the same token. That is correct for a single-tenant deployment and is
explicitly not a credential isolation boundary — storing a per-tenant credential
is what makes it one, and phase 4 will fill that store from an OAuth install flow
instead of a paste box.

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

## Many repositories, many providers: connections

> **As a human planner**, I want my board to hold work across several
> repositories — some on GitHub, some on our own GitLab — **so that** planning a
> feature that touches two services does not mean two boards or a reconfigured
> one.

> **As an operator**, I want each host configured once, with its own credential,
> **so that** rotating the GitLab token cannot break GitHub and a leaked token has
> one blast radius I can name.

> **As an agent driving the board over MCP**, I want to list the repositories a
> tenant has and dispatch a card to a named one, **so that** I do not have to
> guess which repo "the" configuration currently points at.

Before this, a tenant had one configuration row: one provider, one host, one
project, one credential. The three provider buttons in Settings were therefore a
**choice**, not three connections — picking GitLab reconfigured the board away
from GitHub. Two levels replace it.

### What a connection is, and what a repository is

A **connection** is a place the board can authenticate to:

- a **provider** (`github`, `gitlab`, `azure_devops`),
- a **host** (`base_url`) — the field that makes a self-hosted GitHub Enterprise,
  GitLab or Azure DevOps Server work,
- a **credential**, encrypted at rest and never returned by anything,
- a **label**, so a human can tell two of them apart,
- and whatever the last **verify** proved.

A **repository** is something to work on *through* a connection:

- a **project** path (`owner/repo`, `group/subgroup/project`,
  `organization/project/repo`),
- optionally **import from** (`intake_project`) — where the import reads issues
  when that differs from the project above,
- an **AIFactory project id** — which AIFactory project a card for this repository
  is *built* in,
- **default labels** put on issues the board opens for it.

The split is not cosmetic. A credential authenticates to a **host**, so two
repositories on the same host are reached with the same token: keeping it on the
connection means rotating it once and leaking it from one row. Verification works
the same way — what a verify proves is "this host answers and accepts this
credential", which it demonstrates by reading one of the connection's
repositories.

A tenant may have as many connections as it likes, and **no two of them may name
the same (provider, host)**. Configuring github.com twice is a mistake, not a
feature: it would leave "which credential reaches github.com?" without a single
answer.

### What happens to a card that names no repository

Every tenant with any repository has **exactly one default**, and the database
enforces that — not an application check that two concurrent writes could slip
past.

A card carries an optional `repository_id`. A card that does not name one — which
is every card created before this phase, and every card a human does not pick a
repository for — resolves in this order:

1. its own `repository_id`, if it has one;
2. the repository whose project path matches the card's `issue_ref` — so a card
   imported from a GitLab repo syncs back to *that* GitLab repo even when the
   tenant's default is on GitHub;
3. **the tenant's default repository.**

That default decides where its issue is opened, which host and credential are
used, and which AIFactory project its build lands in. So a board with one
repository behaves exactly as it did before this phase: one default, and nothing
to choose.

If a tenant has **no** repositories at all, resolution falls back to the
deployment's environment variables, which is the same one-release bridge phase 2
introduced.

### Every option, and its default

| Setting | Level | Type | Default | What it decides |
|---|---|---|---|---|
| **Provider** | connection | `github` \| `gitlab` \| `azure_devops` | `github` | Which host implementation this connection talks through. |
| **Host** (`base_url`) | connection | http(s) origin | the provider's public default (`https://api.github.com`, `https://gitlab.com`, `https://dev.azure.com`) | Where this connection's API calls go. |
| **Label** | connection | string, max 128 | the provider name (`github`) | The human name in the cockpit. Cosmetic — nothing addresses a connection by it. |
| **Credential** | connection | write-only string | unset (falls back to the deployment's environment token) | What this connection authenticates with. Encrypted, sealed against **this tenant and this connection**, never returned. |
| **Status** | connection | derived, read-only | `unconfigured` | `unconfigured` (no repositories yet) -> `credential_missing` (no usable credential, or one the host refused) -> `configured` (reachable in principle, never proved) -> `verified` (proved by `:verify`). |
| **Project** | repository | provider path | required | Where a card for this repository opens its issue. |
| **Import from** (`intake_project`) | repository | provider path | falls back to **Project** | Where an import reads issues for this repository. |
| **AIFactory project id** | repository | project id (a UUID in practice) | unset | Which AIFactory project a card for this repository is built in. Not a repository path — see [The AIFactory project id](#the-aifactory-project-id-in-full). |
| **Default labels** | repository | list of strings | empty | Labels put on issues the board opens for this repository. A `factory:<tier>` label is refused (RFC-0011 intake trigger). |
| **Default** (`is_default`) | repository | boolean, one per tenant | the FIRST repository a tenant creates | Whether a card that names no repository resolves here. |
| `repository_id` | card | integer or null | null = the tenant default | Which repository this card is for. |

### What happens when it is unset or wrong

| Situation | What happens | Loud or quiet |
|---|---|---|
| A card names no repository | It uses the tenant's default. This is the normal case, not a degraded one. | Quiet by design |
| A card names a repository that has since been deleted | It falls back to the tenant default, exactly like a card that never named one. Planning data is never deleted because a repository was. | Quiet |
| The tenant has repositories but no default | Only reachable by clearing the project on the legacy single-configuration form. A card naming none reads `unconfigured` and opens no issue; every repository stays intact and addressable by id. | Visible in the panel |
| The default repository is deleted | The tenant's **oldest remaining** repository is promoted, so a tenant with repositories always has a default. | Quiet, reported in the response |
| A connection is deleted | Its repositories **and its credential** go with it — a repository cannot be reached without its host. Cards are not deleted; they fall back to the default. | Loud in the response |
| Two repositories on different connections share a project path | The card's `issue_ref` cannot tell them apart, so the **default** wins the tie; name `repository_id` on the card (or `repository_id` on the import) to be explicit. | Quiet — pass the id |
| The same (provider, host) added twice | **400** naming the existing connection. Edit that one, or add a repository to it. | Loud, at save time |
| The same project added twice to one connection | **400**. Adding a repository twice to one host is a mistake, not two repositories. | Loud, at save time |
| A project path the connection's provider cannot address | **400** naming the expected shape, at save time rather than on every later card write. | Loud, at save time |
| A connection with no repositories is verified | `ok: false` with `status: unconfigured` — there is nothing on the host to read. | Loud, in the response |
| A connection's provider or host is changed | Its verification is cleared (it proved a different connection); the credential is **kept**, because it is bound to the connection's identity and not to its host. Renaming the label clears nothing. | Quiet, visible as the status dropping to `configured` |

### The credential, per connection

Everything RFC-0020 section 3.4 guarantees still holds, now one level down. The
credential is encrypted with a per-record data key wrapped by the deployment's
`CFACTORY_CREDENTIAL_KEY`, it is refused rather than stored when no key is
configured, and no endpoint, tool or panel returns it — a read is told only
*whether* there is one, when it was stored and which key wraps it.

What phase 8 added is the binding: the associated data on both crypto layers now
covers **the tenant and the connection**. A sealed record lifted onto another
connection does not decrypt, including another of the same tenant's — so database
access alone does not let a GitLab token be replayed as the GitHub one.

### Upgrading an existing tenant

Nothing to do. On the first boot after this release every existing single
configuration is **adopted**: one connection (provider, host, verify state) plus,
if it named a project, one repository marked as that tenant's default, carrying
the project, import project, AIFactory project id and default labels. The adoption
is idempotent, runs for every tenant in the database rather than waiting for
someone to log in, and never overwrites an edit made afterwards.

The stored credential is **re-sealed onto its new connection in memory** — it is
never written out, logged, or returned in the process. If the deployment happens
to boot without its encryption key, the record keeps its previous binding and is
re-sealed on the first read once the key is back; it is never deleted, because a
missing key must not destroy a credential.

### The surface

| Method | Path | Scope | MCP twin |
|---|---|---|---|
| `GET` | `/api/tenants/{tenant}/git-connections` | read | `cfactory_list_git_connections` |
| `POST` | `/api/tenants/{tenant}/git-connections` | write | `cfactory_create_git_connection` |
| `PATCH` | `/api/tenants/{tenant}/git-connections/{connection_id}` | write | `cfactory_update_git_connection` |
| `DELETE` | `/api/tenants/{tenant}/git-connections/{connection_id}` | write | `cfactory_delete_git_connection` |
| `POST` | `/api/tenants/{tenant}/git-connections/{connection_id}:verify` | write | `cfactory_verify_git_connection` |
| `PUT` | `/api/tenants/{tenant}/git-connections/{connection_id}/credential` | write | `cfactory_set_git_connection_credential` |
| `DELETE` | `/api/tenants/{tenant}/git-connections/{connection_id}/credential` | write | `cfactory_delete_git_connection_credential` |
| `GET` | `/api/tenants/{tenant}/git-repositories` | read | `cfactory_list_git_repositories` |
| `POST` | `/api/tenants/{tenant}/git-connections/{connection_id}/repositories` | write | `cfactory_create_git_repository` |
| `PATCH` | `/api/tenants/{tenant}/git-repositories/{repository_id}` | write | `cfactory_update_git_repository` |
| `DELETE` | `/api/tenants/{tenant}/git-repositories/{repository_id}` | write | `cfactory_delete_git_repository` |
| `POST` | `/api/tenants/{tenant}/git-repositories/{repository_id}:default` | write | `cfactory_set_default_git_repository` |

`GET /git-connections` returns each connection with its repositories inline plus
`default_repository_id`, so one call renders the whole panel.
`GET /git-repositories?connection_id=` is the flat list — "everything a card can
be dispatched to".

`POST /api/cards/import` and `cfactory_import_cards` take `repository_id` (or a
`project` path), which is what makes **import per-repository**: the host and
credential come from that repository's connection, and imported cards are stamped
with it. A tenant with four repositories across two providers imports from all
four by calling it four times.

Tenant isolation is unchanged and applies to ids as well as names: a connection or
repository belonging to another tenant is **404 Not Found**, never 403 — a 403
would confirm that the id exists. The MCP tools take no tenant argument at all.

### Recommended

For a single-repository board, do nothing: one connection, one repository, and it
is the default. That is what the adoption leaves you with and what the legacy
Settings form keeps editing.

For anything larger:

1. **One connection per host**, labelled for humans (`Work GitHub`,
   `Self-hosted GitLab`), each with its own credential, and `:verify` each once.
2. **One repository per repo you actually plan work in.** Do not add a repo
   "for later" — an unused repository is another thing to keep a project id right
   in.
3. **Make the repository you plan in most the default**, since that is where every
   card that does not choose ends up.
4. **Set `repository_id` on a card as soon as you know it**, rather than relying on
   the default. It is what makes the card's issue, its import and its build all
   land on the same host.
5. **Give each repository its own AIFactory project id.** Sharing one across
   repositories puts two codebases' builds in one project, which is the state the
   fleet's own dashboards cannot untangle later.

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
| Name another tenant's connection or repository id | 404 Not Found, never 403 — a 403 would confirm the id exists | Loud |
| Add the same provider + host twice | 400 naming the connection you already have | Loud, at save time |
| Create a card with no `repository_id` | It uses the tenant's default repository | **Silent — and intended.** It is the normal case. |
| Delete the repository a card pointed at | The card survives and falls back to the tenant default | Silent — planning data is never destroyed by a config change |
| Delete a connection | Its repositories AND its credential go with it | Loud in the response |
| Verify a connection with no repositories | `ok: false`, `status: unconfigured` — nothing on the host to read | Loud |
| Move a credential's row onto another connection in the database | It stops decrypting — the connection is bound into the ciphertext | Loud, as `credential_missing` |
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

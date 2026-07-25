---
layout: default
title: Plan a card, watch it build
permalink: /guides/plan-a-card-walkthrough/
---

# Plan a card, watch it build

A start-to-finish run of the RFC-0019 planning board: write a card, promote it,
watch the factory pick it up, and see the card move on its own. Roughly fifteen
minutes, no prior CFactory knowledge assumed.

> **As a newcomer to the fleet**, I want one path that goes all the way from an
> empty board to a running build, **so that** I understand how planning and
> execution connect before I have to configure either of them.

Every step says what you should see, and what it means if you see something
else. For the reference material behind any step, see
[The agent-native planning board](planning-board.md).

---

## Before you start

You need three things.

**1. A running CFactory.** Backend on `:3111`, cockpit on `:3110` by default.

**2. A credential, or deliberate open mode.** Locally, leaving
`CFACTORY_API_KEYS` unset means the REST API runs OPEN and you need no header at
all. The MCP endpoint is different — it fails **closed**, so if you want to do
the agent half of this walkthrough you must set one of:

```bash
CFACTORY_MCP_DEV_OPEN=true          # local only, never in a shared deploy
CFACTORY_MCP_SECRET=some-dev-secret # or a real credential
```

**3. An intake destination.** Decide now which half of the walkthrough you are
doing:

| If you have | Use tier | Extra config |
|---|---|---|
| A reachable AIFactory and a project id | `low` or `medium` | `CFACTORY_INTAKE_PROJECT_ID=<project uuid>` |
| A reachable PFactory only | `hard` | none |
| Neither (just exploring the board) | leave tier unset | none — the card will simply never dispatch |

For the hosted deployment the intake project is already set to `aifactory-demo`
(`5d78d4b9-35f9-4445-92c1-78f3ff60a494`), chosen because it is the sacrificial
demo repo — an autonomous build writes real code, so the default destination
should be somewhere a wrong build costs nothing. See
[`CFACTORY_INTAKE_PROJECT_ID` in full](planning-board.md#cfactory_intake_project_id-in-full).

Throughout, `$CF` is your CFactory base URL:

```bash
CF=http://localhost:3111
```

---

## Step 1 — Look at the empty board

```bash
curl -s "$CF/api/cards" | jq
```

Expected:

```json
{"count": 0, "cards": []}
```

In the cockpit, the **Backlog** and **Planning board** views show the same
nothing. If you instead get a 401, your keystore is configured and you need
`-H "Authorization: Bearer <key>"` on every call below.

Note which board you are looking at. **Planning board** columns are `backlog /
ready / in_progress / blocked / done` — the planning axis. The pre-existing
**Board** view has plan / code / test columns and is the *execution* axis over
work items. They are different data joined by one key, and you will see both
populate later in this walkthrough.

---

## Step 2 — Write a card

Do not set a tier yet. A card with a tier is one edit away from starting a
build, and you want to write the acceptance criteria first.

```bash
curl -s -X POST "$CF/api/cards" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Add a /healthz endpoint that reports build SHA",
    "acceptance_criteria": [
      "GET /healthz returns 200 with {\"status\":\"ok\",\"sha\":\"<git sha>\"}",
      "The SHA comes from build-time env, not a runtime git call",
      "A unit test asserts the shape of the payload"
    ],
    "milestone": "v0.3"
  }' | jq
```

Expected: **201**, and a card with `"card_key": "FCT-1"`, `"status": "backlog"`,
`"priority": 0`, `"tier": null`, `"correlation_key": null`.

What just happened:

- You omitted `card_key`, so the store assigned the next `FCT-<n>` for your
  tenant. Supplying one that already exists would have been a 409, never a
  silent overwrite.
- `correlation_key` is `null`. That is the entire meaning of "planned but not in
  the factory", and it is also the idempotency guard you will rely on in step 5.
- The acceptance criteria are not decoration. They become an
  `## Acceptance Criteria` bullet list in the brief sent upstream — an empty list
  means the factory gets a title and nothing else to build against.

Refresh the cockpit. The card is in the **Backlog** list and in the `backlog`
column of the **Planning board**.

---

## Step 3 — Do the same thing as an agent

This is the step that shows why the board exists. Point an MCP client at
`$CF/mcp`:

```json
{
  "mcpServers": {
    "cfactory": {
      "type": "http",
      "url": "http://localhost:3111/mcp",
      "headers": {"Authorization": "Bearer ${CFACTORY_MCP_TOKEN}"}
    }
  }
}
```

Then ask it to list the backlog. It calls `cfactory_list_cards` and sees
`FCT-1` — the same row, from the same table, with the same values.

Have it add one:

```
cfactory_create_card {
  "title": "Document the /healthz payload in the API reference",
  "acceptance_criteria": ["The reference lists status and sha with example values"],
  "milestone": "v0.3",
  "priority": 10
}
```

Now `curl -s "$CF/api/cards" | jq '.count'` returns `2`, and the cockpit shows
both. There is one implementation underneath both surfaces (`card_ops.py`), so
this is not two systems agreeing — it is one system with two doors.

If the tool call comes back `403 MCP token lacks required scope: 'write'`, your
key is read-only. That is the scope model working: a read key can enumerate the
whole backlog and change none of it.

If it comes back `401 MCP is not configured`, nothing is set and the endpoint is
failing closed. Go back to the prerequisites.

Note the priority: `10` on the second card puts it **below** `FCT-1` (which is
`0`), because lower sorts first. Leaving gaps like this lets you insert between
cards later without renumbering.

---

## Step 4 — Promote it, and start a build

This is the intake trigger: **`status: ready` plus a tier**. One PATCH does
both.

```bash
curl -s -X PATCH "$CF/api/cards/FCT-1" \
  -H 'Content-Type: application/json' \
  -d '{"status": "ready", "tier": "low"}' | jq
```

Expected: **200**, and — this is the part that surprises people — the card comes
back with `"status": "in_progress"` and a **non-null `correlation_key`**, not
`ready`. You asked for `ready`; the intake hook fired inside the same request,
dispatched the card, and the response is the card as it now stands.

Which door it went through depends on the tier you set:

| Tier | Where it went | What it needed |
|---|---|---|
| `low` / `medium` | AIFactory `POST /api/tasks/from-issue` — the skip-planning fast path | `CFACTORY_INTAKE_PROJECT_ID` |
| `hard` | PFactory `POST /api/plan/sessions/ingest-text` — full decomposition first | nothing extra |

### If the card came back `blocked` instead

That is the loud failure path, and the reason is in the response. The two you
will actually hit:

- **`no intake project configured — set CFACTORY_INTAKE_PROJECT_ID to dispatch a
  low/medium card to AIFactory`.** AIFactory's `from-issue` needs a `project_id`
  and a card carries none, so it comes from deployment config. Set it and PATCH
  again — the card is still `blocked` with a null `correlation_key`, so it is
  fully re-promotable.
- **An upstream status code in the audit entry.** The dispatch was attempted and
  AIFactory or PFactory rejected it — bad project id, service down, bad
  credential. Check `CFACTORY_UPSTREAM_TOKEN` and that the relevant
  `CFACTORY_*_API_URL` points somewhere real.

A blocked card is never a lie: it did not enter the factory, and it is not left
sitting in `ready` pretending it did.

### If nothing happened at all

The card is `ready` and `correlation_key` is still `null`? You did not set a
tier. A `ready` card with no tier is a legitimate board state — "queued for
triage" — so it is deliberately left alone. This is the one intentional silent
no-op in the whole feature. Set a tier and PATCH again.

---

## Step 5 — Confirm you cannot double-build it

```bash
curl -s -X PATCH "$CF/api/cards/FCT-1" \
  -H 'Content-Type: application/json' \
  -d '{"status": "ready"}' | jq
```

The card does not dispatch a second time. There is no "dispatched" column
anywhere: **`correlation_key` non-null *is* "already in the factory"**, so a
re-promotion is a no-op. Click twice, retry a failed request, race a REST call
against an MCP call — you still get one build.

---

## Step 6 — Watch it on the other board

Take the `correlation_key` from step 4:

```bash
curl -s "$CF/api/workitems/$CORR" | jq '{correlation_key, pfactory, aifactory, tfactory}'
```

Or ask the MCP client for `cfactory_get_workitem` with that key, or
`cfactory_get_timeline` for the ordered event sequence.

Now open both cockpit views side by side:

- **Planning board** — `FCT-1` sits in `in_progress`. This is *what you decided
  to do*.
- **Board** — the same work appears as a work item moving through the plan /
  code / test stages. This is *how far it has actually got*.

One key joins them. That is the whole architecture: the planning axis and the
execution axis, threaded rather than merged.

---

## Step 7 — Watch the card move on its own

Do not touch the card. As completion events arrive from the factories, the same
stream that threads the work-item timeline writes the card's status:

| The factory reports | Card becomes |
|---|---|
| Any stage failed or stuck | `blocked` |
| TFactory or AIFactory stage done | `done` |
| **PFactory stage done** | **`in_progress`** (not `done`) |
| Anything else in flight | `in_progress` |

That third row is worth pausing on, and it is the reason a `hard` card behaves
differently from a `low` one. When PFactory finishes, the plan is ready and **no
code has been written**. A finished plan is not a verdict, so the card stays
`in_progress` and waits for the build. Only a verdict from further down the
pipeline closes it.

Poll the card and watch:

```bash
watch -n5 "curl -s $CF/api/cards/FCT-1 | jq '{status, correlation_key}'"
```

Once a card is joined, the pipeline owns its status. If you manually drag a live
card somewhere, the next completion event puts it back where the factory says it
is. That is the board being a live view rather than a copy someone maintains.

---

## Step 8 — See what an agent sees before it authenticates

Every step so far needed a credential. This one does not:

```bash
curl -s "$CF/.well-known/agent-skills/index.json" | jq '.skills[].name'
curl -s "$CF/.well-known/agent-skills/fleet.json"  | jq '{services: [.services[].service.name], unavailable: [.unavailable[].name]}'
```

The first is CFactory's own manifest — capability names derived live from the
MCP tool catalogue, so it cannot drift from the server. The second folds in
PFactory's, AIFactory's and TFactory's manifests too, which is how an agent
learns what the *whole* fleet can do in one unauthenticated GET.

Siblings you have not got running show up in `unavailable[]` with a reason
rather than being omitted — so an agent learns the service exists and is
temporarily undiscoverable, instead of concluding the fleet is smaller than it
is. Nothing is invented for them: no version, no endpoint, no skills.

These paths are unauthenticated **by construction** — the API-key middleware
guards `/api/*` and `/connect/*` only — and carry public metadata exclusively.
No tokens, no internal hostnames, no card or work-item data.

---

## Step 9 — Clean up

```bash
curl -s -X DELETE "$CF/api/cards/FCT-1" | jq
curl -s -X DELETE "$CF/api/cards/FCT-2" | jq
```

Deletion is permanent and does not touch the work item — the build you started
keeps running. Every one of these mutations, including the deletes, appended an
entry to the tamper-evident HMAC audit chain, recording who did it and whether
it arrived over `/api/cards` or over `/mcp`.

---

## What you just proved

- A card is planning intent, in its own table, safe from the pruning machinery
  that keeps the work-item mirror honest.
- A human and an agent perform the *same* operations, because there is one
  implementation with two doors — and `tests/test_board_parity.py` fails CI if
  anyone adds one door without the other.
- Promoting to `ready` with a tier is the single intake trigger, whichever
  surface or verb produced it, and it cannot fire twice.
- A dispatch that cannot happen produces a `blocked` card with a reason, not a
  silent drop.
- Once joined, the pipeline writes the card's status — and refuses to call a
  finished plan "done".

Next: [The agent-native planning board](planning-board.md) for every field,
option and failure mode, or the
[Environment reference](../dev/environment-reference.md) for the variables.

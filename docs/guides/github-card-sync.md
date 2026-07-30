# GitHub Card ↔ Issue Sync

A planning card on the CFactory board can be backed by a GitHub issue. The
feature is **off by default** and is enabled by one credential.

## The rule: GitHub wins

**GitHub issues and PRs are the record of truth (RFC-0003). The board is a
planning projection that syncs to GitHub.** There is no new source of truth.
Humans plan on the board; the work lives in GitHub.

Everything else follows from that. Each card field belongs to exactly one side —
there is no field both sides own, which is what keeps conflict resolution a rule
rather than a merge algorithm:

| Field | Owner | On conflict |
|---|---|---|
| `title` | GitHub | **GitHub wins** — the issue's title overwrites the card's. |
| `labels` | GitHub | **GitHub wins** — mirrored down, never pushed up. |
| `issue_state` (open/closed) | GitHub | **GitHub wins** — mirrored down. |
| `status` (board column) | shared at the `done` end | Closed issue → card `done`. Reopened issue while the card says `done` → card `in_progress`. Any other column on an open issue is the board's own. |
| `priority`, `tier`, `milestone`, `acceptance_criteria`, `assignee` | the board | Never touched by a sync — GitHub has no opinion on them. |

So if you rename a card and someone else renames the issue, the issue's title is
what you will have after the next sync. That is the intended behaviour, not data
loss to be fixed: the board is not allowed to assert something GitHub never said.

The rule is implemented once, in `apps/backend/cfactory/github_sync.py`, and used
by both the REST route and the MCP tool.

### The import mirrors MORE than the per-card sync

The table above is the per-card sync (`POST /api/cards/{card_key}/sync-github`).
The repository import — and therefore the background poll, which is the same code
— owns a wider set, because it is populating cards from issues rather than
reconciling one card a human already owns:

| Field | Per-card sync | Import / poll |
|---|---|---|
| `title`, `description`, `labels`, `issue_state` | host wins | host wins |
| `status` | host wins at the open/closed ends | host wins, **except** on a card the factory has touched, which keeps its column |
| `tier` | untouched | **host wins** — derived from the `factory:<tier>` label, and the labels are the host's |
| `assignee`, `milestone` | untouched | **host wins** |
| `priority` | untouched | set once at import, never mirrored again |
| `acceptance_criteria`, `correlation_key`, `stage_runs` | untouched | untouched |

The mirrored set is one constant in the code —
`issue_import.MIRRORED_FIELDS` — with a test that fails if the mapping and the
constant drift apart, so this table is not a hopeful description of the behaviour.

**What this costs you.** An edit made on the board to a mirrored field is
overwritten on the next poll, silently and by design. Two writers with no merge
rule means one of them has to lose, and the one that loses is the board: the
repository is where the issue is discussed, closed and reopened, and a board that
could win would silently contradict it. Concretely, and worth knowing before it
surprises you:

- reassign a card on the board while the issue is unassigned, and the next poll
  clears the assignee — "the host wins" includes the host having nothing to say;
- set a tier on the board without a `factory:<tier>` label on the issue, and the
  next poll clears the tier;
- retitle a card, and the next poll restores the issue's title.

**Edit the issue, not the card**, for anything in the mirrored set. Priority,
acceptance criteria and the pipeline's own records are the board's and survive
every pass — which is where board-side planning belongs.

If a deployment genuinely wants board-owned assignment or milestones, the change is
to remove those fields from `MIRRORED_FIELDS` and from `_mapping` in
`apps/backend/cfactory/issue_import.py` — a deliberate, reviewable decision to move
the boundary, not something to discover by finding your edits gone.

## Enabling it

Two halves, and since RFC-0020 section 3.3 they live in different places.

**The credential is the deployment's**, from the environment:

```bash
CFACTORY_GIT_PROVIDER_TOKEN=<a token that can open issues>   # required; unset = sync OFF
```

The bare `GITHUB_TOKEN` / `GH_TOKEN` are deliberately **not** read. This
credential files issues in a real repository, and an ambient `gh` login on a
developer's machine must not be able to switch that on by accident.
`CFACTORY_GITHUB_TOKEN` is the older name and still works.

**Which host and which project is the tenant's**, edited in the cockpit at
**Settings > Git integration** (or over `PUT /api/tenants/{tenant}/git-config`,
or the `cfactory_set_git_config` MCP tool):

| Setting | Example | Meaning |
|---|---|---|
| Provider | `github` | `github` (default), `gitlab` or `azure_devops` |
| Host | `https://api.github.com` | Override for GitHub Enterprise or a self-hosted GitLab |
| Project | `owner/repo` | Where a `ready` card opens its issue |

With a token but no project, cards can only *adopt* existing issues — nothing is
ever created.

`CFACTORY_GITHUB_REPO`, `CFACTORY_GIT_PROVIDER`, `CFACTORY_GIT_PROVIDER_URL` and
`CFACTORY_INTAKE_PROJECT_ID` still **seed** a tenant that has no stored
configuration, once, on first boot — so an existing deployment keeps working with
no operator action and finds its values already filled in. They are removed one
release from now. The full write-up, with every option and its failure
behaviour, is in
[the planning-board guide](planning-board.md#git-integration-the-settings-panel).

## Other git hosts: GitLab and Azure DevOps (RFC-0020 phases 1 and 2)

GitHub is the default, and a board whose provider is never changed behaves
exactly as this guide describes. Point it at another host in **Settings > Git
integration**: pick the provider, give the host if it is self-hosted, and write
the project in that host's own shape — `owner/repo` on GitHub,
`group/subgroup/project` on GitLab, `organization/project/repo` on Azure DevOps.
An unaddressable path is refused when you save it, not on the next card write.

Everything above this section — the ownership table, the conflict rule,
idempotency, adoption, the fail-safe posture — is unchanged, because the board
talks to the fleet's `GitProvider` protocol rather than to any host's API. Read
"GitHub wins" as "the provider wins": a GitLab issue closed on gitlab.com moves
its card to `done` exactly as a GitHub one does, and GitLab's own vocabulary
(`opened` state, IID identifiers, flat label strings) never reaches a card.

That protocol is not CFactory's. It is vendored byte-for-byte from the Factory
hub into `apps/backend/runners/github/` and guarded by the
`factory-github drift` CI gate — do not edit that tree; fix the hub canonical and
re-vendor.

## What syncs, and when

| Trigger | What happens |
|---|---|
| A card write that leaves it `ready` (either surface) | Opens an issue in the tenant's configured **project**, or adopts the one named by `issue_ref`, then mirrors it down. |
| A card write on a card that already has an `issue_ref` | Mirrors the issue down. |
| `POST /api/cards/{card_key}/sync-github` | On-demand sync. |
| MCP `cfactory_sync_card_github` | The same operation, same code (RFC-0019 §3.3). |

**Adopting an existing issue:** set the card's `issue_ref` to `owner/repo#123`
(over `PATCH` or the MCP tools). The card adopts that issue — no new issue is
opened, and no duplicate is possible.

**Idempotency:** `issue_ref` non-NULL *is* "this card already has an issue",
exactly as a non-NULL `correlation_key` means "already in the factory". Syncing
twice adopts and mirrors; it never opens a second issue.

**Issues are opened with the tenant's default labels, and never with a
`factory:<tier>` one.** The fleet's issue-driven intake (RFC-0011) triggers on
that label, and a `ready` card has already been dispatched into the factory by
the board's own intake path — labelling the issue would build the same card
twice. The git-config panel refuses such a label when you save it, so it cannot
reach an issue whatever anyone types. Any other default label (`board`,
`triage`) is applied.

## When GitHub is down

Nothing raises and nothing is silently dropped:

- the failure reason is written to the card's `github_sync_error` and logged;
- the sync result carries `ok: false`, and an `ok=false` entry is appended to the
  audit chain;
- the endpoint returns **200**, not 500 — an unreachable GitHub is not a board
  error, and the board keeps serving;
- no partial write happens: the mirror is computed in full and applied as one
  update, or not at all.

A recovered sync clears `github_sync_error`.

## Known limitation: mirroring is pull-based

There is **no webhook receiver and no poller**. A card learns that its issue was
closed, renamed or relabelled when a sync is *asked for* — the endpoint, the MCP
tool, or a card write that reaches `ready` — not the instant it happens on
github.com. Between syncs a card can be stale, which is why `issue_state` is
stored and displayed rather than inferred.

Live inbound sync needs a public webhook endpoint with signature verification,
which this deployment does not have. That is the follow-up, deliberately not
half-built here.

## Importing a repo's existing issues (RFC-0020 section 3.6)

Sync above is *card-first*: a card can open or adopt an issue, but connecting a
repo brings nothing in. Import is the other direction — the repository's
**existing** issues become cards, so a board that starts empty does not stay
empty for anyone who already has a backlog.

```
POST /api/cards/import            # the whole configured project
POST /api/cards/import?full=true  # ignore the watermark, re-read everything
```

The MCP twin is `cfactory_import_cards` (same arguments, same result), and the
planning board has an **Import repo issues** button. Every import is audited like
any other card write.

It goes through the same provider protocol as the sync, so it works on GitHub,
GitLab and Azure DevOps alike.

### What you get

| Issue | Card | Owner |
|---|---|---|
| `title` | `title` | mirrored (host wins) |
| `body` | `description` | mirrored |
| `factory:<tier>` label | `tier` (`low`/`medium`/`hard` — note `hard`, not `high`) | mirrored |
| other labels | `labels` | mirrored |
| `assignees[0]` | `assignee` | mirrored |
| `milestone` | `milestone` (the title) | mirrored |
| `state` | `issue_state`, and `status` per the rule below | mirrored |
| — | `acceptance_criteria` | **left empty, never parsed from the body** |
| — | `priority` | planning-only, `100` on import |

The body does **not** become acceptance criteria. Those are the testable
statements dispatch turns into the RFC-0002 task contract; parsing prose into
them would fabricate the thing the factory verifies against. An imported card
therefore has none, which is a legal planning state — fill them in before you
promote it.

### An imported card never dispatches

An open issue imports as `backlog`, a closed one as `done`. **Never `ready`.**
`ready` + a tier is the intake trigger and real repositories are full of issues
already labelled `factory:low`, so an importer able to produce `ready` would fire
a build per issue from one click. This is a safety property with a test named
after it, not a default you can configure away.

### Re-running is safe

Import is an upsert against a UNIQUE `(tenant_id, issue_ref)` index, so running
it twice — or twice at the same moment — updates the cards it already created
rather than duplicating them. A card edited locally keeps its planning fields
(priority, acceptance criteria) and loses its mirrored ones to the host, which is
the same "the host wins" rule as the sync.

Pull requests are never imported. `include_prs` is pinned off and is not
configurable: a pull request is not a plan.

### It is a poll. It is not live. It does run on its own.

There is still no webhook receiver, so:

- the first import backfills every open issue (up to `CFACTORY_IMPORT_MAX` —
  truncation is reported, never silent);
- after that a `last_synced_at` watermark exists, and each run asks only for
  issues updated since it, minus a 60-second overlap for clock skew, with the
  state widened to `all` so closures and reopenings are caught;
- that run happens by itself, per repository, every
  `CFACTORY_IMPORT_POLL_SECONDS` — see [Automatic sync](#automatic-sync) below.

An issue filed a second ago is on the board somewhere between zero and one poll
interval later — never instantly. The cockpit shows how long ago each repository
was read for exactly that reason.

## Automatic sync

### The story

> "Do we need to add them, or will they be imported automatically? The point is
> they should be synced automatically, right?"

Yes. You connect a repository, its issues become cards, and from then on the
board keeps itself level with the repository: an issue somebody files, closes,
reopens or retitles turns up on the board without anyone pressing anything. What
you must never have to wonder is whether the board in front of you is a week out
of date, so the planning board carries one line saying how long ago it last read
each repository, plus a **Sync now** button for when you do not want to wait for
the next cycle.

It is a poll, not a webhook. The board is therefore *recent*, never *live*, and it
says which.

### What runs, and what you can set

| Setting | Default | Meaning |
|---|---|---|
| `CFACTORY_IMPORT_POLL` | `true` | The background reconciliation loop. On by default, unlike the cockpit's other background loops: a board that silently drifts is worse than no import, because it looks current. |
| `CFACTORY_IMPORT_POLL_SECONDS` | `300` (5 min) | How often each repository is re-read. Also the width of the poll lease and the basis of the staleness threshold. |
| `CFACTORY_IMPORT_POLL_GAP_SECONDS` | `2` | Pause between two repositories inside one cycle — the rate-limit guard. Forty repositories spread over eighty seconds instead of arriving at the host in one tick. |
| `CFACTORY_IMPORT_MAX` | `1000` | Ceiling per run. Truncation is reported in the result and in the board's summary. |
| `CFACTORY_IMPORT_STATE` | `open` | What the first backfill asks for. The incremental pass always uses `all`. |
| `CFACTORY_IMPORT_LABELS` | *(empty)* | Optional label filter for the backfill. Opt-in narrowing, never a default. |

One cycle does this, per repository the tenant has, one repository at a time:

1. **Claims a lease** on that repository (`card_import_state.poll_leased_until`,
   held for half a cadence). A second replica waking at the same moment finds the
   lease taken and skips — the board is unharmed either way, since the unique
   `(tenant_id, issue_ref)` index makes a double import impossible, but the
   provider is spared the duplicate read.
2. **Reads the changes** since that repository's watermark — one API call.
3. **Records `last_polled_at`**, whether or not anything changed. A quiet
   repository is still a synced repository.
4. **Waits `CFACTORY_IMPORT_POLL_GAP_SECONDS`** before the next repository.

A repository whose read fails sits out cycles rather than being asked again at
full cadence: one cycle, then two, then four, capped at eight (forty minutes at
the default), cleared by the first success. A `429` or an explicit rate-limit
refusal starts at four cycles rather than one, because the host has just said
"fewer requests" and one cycle later is not an answer to that. Recovery needs no
operator action.

### Seeing whether the board is current

The planning board shows one line above the columns — "Synced 2 min ago — polls
every 5 min, not live" — and a **Sync now** button beside it that reads every
connected repository immediately. The same data is available to agents and scripts:

```bash
curl -s $CFACTORY_URL/api/cards/sync-state | jq
```

```json
{
  "now": "2026-07-26T12:00:00+00:00",
  "poll": { "enabled": true, "interval_seconds": 300.0, "live": false },
  "repositories": [
    {
      "repository_id": 1,
      "project": "acme/widgets",
      "is_default": true,
      "last_polled_at": "2026-07-26T11:58:04+00:00",
      "watermark_at": "2026-07-20T10:00:00+00:00",
      "stale": false
    }
  ]
}
```

`last_polled_at` is when that repository was last read successfully — the answer to
"can I trust this board?". `watermark_at` is the incremental cursor and is a
different thing: on a repository nobody has touched for a month it stays a month
old however often the poll runs, so do not read staleness off it. `stale` is the
server's rule (no successful read for more than two cadences) so the cockpit, the
MCP tool `cfactory_card_sync_state` and anything you write agree on it.

### Unset, off, or wrong

| Situation | What happens | How you find out |
|---|---|---|
| `CFACTORY_IMPORT_POLL=false` | Nothing reconciles. Cards are only imported when a human presses Sync now or something calls `POST /api/cards/import`. | The board's sync line says "automatic sync is OFF (CFACTORY_IMPORT_POLL)", and `poll.enabled` is `false`. |
| No repository configured, or no credential | Every cycle is a no-op: nothing is resolved and no request is made. | The sync line says "No repository connected — nothing to sync". |
| Provider down, credential rejected, project renamed | The board keeps serving reads with the cards it already has. The import returns HTTP 200 with `ok: false` and the reason; the failing repository backs off and retries. | Nothing turns stale for two cadences, then the sync line says STALE. A manual Sync now returns the reason verbatim. |
| `CFACTORY_IMPORT_POLL_SECONDS` very low (say 5) | The board is more current and every repository is read twelve times a minute. Rate limits are a real ceiling: GitHub allows 5000 authenticated requests an hour, so 40 repositories at 5 seconds is roughly 29 000 an hour and will be throttled. | 429s, then the backoff, then a board that is *less* current than at the default. |
| `CFACTORY_IMPORT_POLL_GAP_SECONDS=0` | Pacing off — every repository in a cycle is read back to back. Fine for one or two repositories, a burst at forty. | Nothing, until the host throttles you. |
| More than one replica | Each cycle, each repository is read by whichever replica claims its lease. Worst case (a lease that expires between two closely spaced replicas) is a duplicated read, never a duplicated card. | `poll_leased_until` on `card_import_state`. |
| An issue arrives while a cycle is running | It is picked up next cycle. So is a repository connected mid-cycle: the target list is re-read every time, so connecting a repository never needs a restart. | It appears within one cadence. |

### Recommendation

Leave `CFACTORY_IMPORT_POLL=true` and the cadence at `300`. Five minutes is well
inside what planning work notices, and at one cheap incremental call per
repository it is nowhere near any provider's limits. If you have many
repositories, raise `CFACTORY_IMPORT_POLL_SECONDS` before you lower
`CFACTORY_IMPORT_POLL_GAP_SECONDS` — a slower cycle costs you freshness you can
measure, while an unpaced burst costs you a throttled credential that makes
everything else fail too. Turn the poll off only for a deployment that imports
deliberately, on someone's command, and expect to explain to its users why the
board is behind.

The poll is the backstop by design. If a webhook receiver is added later it
becomes the low-latency path and this stays underneath it, because webhook
deliveries are lost and a board that misses one never recovers without a poll.

### Closing, deleting, disappearing

- Issue closed on the host -> card `done`; reopened -> back to `backlog`.
- A card already **in the factory** (non-NULL `correlation_key`) keeps its
  `status`: a run in flight owns it, and a poll must not stomp it back to
  `backlog` because somebody reopened the issue. Everything else still mirrors.
- Deleting a card is a **soft delete**. The issue is not touched — deleting a
  card means "not on my board", never "destroy the record of truth" — and the
  next import does not resurrect it.
- An issue deleted or transferred on the host answers 404, which sets
  `issue_state: missing`. The card stays: human planning data is not destroyed
  by a 404.

## Issue comments (Factory#375)

### The story

> "What about the details and the comments we need those as well."

An imported card carried the issue's title, body, labels and state — and none of
its discussion. For planning that is the wrong half. A body says what somebody
wanted on the day they filed it; the thread underneath is where it was argued
about, narrowed, deferred or decided. Reading a card without it means re-deciding
something the repository already settled.

So the comments come in with the issue. Each card carries how many it has, and
opening the card shows the thread — collapsed by default, because a backlog of
forty-six cards each rendering a full discussion is worse than the board that had
no comments at all.

They are **stored**, not fetched when you open a card. That was a real choice.
Fetching on open is always current and costs a provider call per glance; storing
makes the thread searchable, survives a provider outage, and needs something to
keep it fresh — which is exactly what the automatic sync above became. Storing a
stale copy with no refresher would have been the worst of both, and before that
poll existed it would have been the only option.

### What runs, and what you can set

| Setting | Default | Meaning |
|---|---|---|
| `CFACTORY_IMPORT_COMMENTS` | `true` | Import and refresh issue comments alongside the issues themselves. |
| `CFACTORY_IMPORT_COMMENT_BACKFILL_MAX` | `25` | How many never-read cards one pass may backfill. Bounds the only unbounded path; the rest arrive on later passes. |

Comments ride the same pass as the issues — the manual `POST /api/cards/import`
and the background poll both — on the same connection, resolved through the card's
own repository. There is one sync path, not two: a card on a GitLab repository has
its comments read from GitLab, never from the tenant's GitHub connection.

Each pass splits the repository's cards in two:

- **Cards with a complete copy** are refreshed in ONE request that asks only for
  what changed since the oldest copy in the group. On GitHub that is the
  repository-wide comments endpoint with a server-side `since`, so refreshing
  fifty-five cards costs the same single call as refreshing one.
- **Cards never read** have no window to narrow, so each costs a call. That is
  what `CFACTORY_IMPORT_COMMENT_BACKFILL_MAX` bounds — a freshly connected
  200-issue repository fills in over consecutive passes instead of firing 200
  requests in one tick.

So a steady-state cycle on a 55-card GitHub repository is **two API calls**: one
page of issues, one page of comments. A cold start is 25 comment calls per pass
until the backlog is covered.

GitLab and Azure DevOps have no repository-wide comments endpoint — neither vendor
offers one — so on those hosts a refresh is one call per card. That is bounded and
predictable rather than cheap; if it is not a trade you want, set
`CFACTORY_IMPORT_COMMENTS=false`.

### Reading them

The card itself carries two scalars, not the thread:

```json
{ "card_key": "FCT-42", "comment_count": 3, "comments_synced_at": "2026-07-26T12:00:00+00:00" }
```

The bodies are their own read, so listing a backlog never ships forty-six
discussions:

```bash
curl -s $CFACTORY_URL/api/cards/FCT-42/comments | jq
```

```json
{
  "card_key": "FCT-42",
  "issue_ref": "acme/widgets#7",
  "count": 1,
  "synced_at": "2026-07-26T12:00:00+00:00",
  "comments": [
    {
      "comment_id": "101",
      "author": "reviewer",
      "body": "Storing beats fetching now that the poll exists.",
      "url": "https://github.com/acme/widgets/issues/7#issuecomment-101",
      "created_at": "2026-07-20T11:00:00+00:00",
      "updated_at": "2026-07-20T11:00:00+00:00"
    }
  ]
}
```

The MCP twin is `cfactory_card_comments`, under the same parity law as every other
board capability: an agent planning from a card reads the discussion the same way
a human does.

### `synced_at` is the honesty field

**An empty thread and a failed download are not the same thing, and are never
reported as the same thing.**

- `count: 0` beside a `synced_at` timestamp means the issue genuinely has no
  discussion. That was read successfully and is a real answer.
- `count: 0` beside `synced_at: null` means the board does not know — the thread
  has never been read, or the last read failed.

A failed fetch stores nothing and moves no marker, so a card whose comments could
not be downloaded keeps saying "unknown" until a later pass succeeds. It is never
recorded as a quiet issue. A card that already had a complete copy keeps it
through an outage, with its old timestamp, so what you are looking at is complete
as of a time you can see.

`comment_id` is the provider's own id, and re-import is idempotent on it: an edited
comment updates the row it already has and can never become a second copy.

### Unset, off, or wrong

| Situation | What happens | How you find out |
|---|---|---|
| `CFACTORY_IMPORT_COMMENTS=false` | Issues still import. No comment call is made and no card claims a thread it never read. | Every card reports `comments_synced_at: null` and no count. |
| Comment read fails (provider down, credential rejected, endpoint 404) | The issue import still succeeds — a comment outage must not fail the board. Nothing is stored and no marker moves. | The import result carries `comments.ok: false` with the reason; affected cards keep `comments_synced_at: null`, or their previous timestamp. |
| A freshly connected repository with hundreds of issues | Comments fill in `CFACTORY_IMPORT_COMMENT_BACKFILL_MAX` cards per pass. Cards not yet reached show no thread rather than an empty one. | `comments_synced_at` is null on the cards still queued; the count rises each cycle. |
| `CFACTORY_IMPORT_COMMENT_BACKFILL_MAX=0` | Nothing is ever backfilled, so nothing is ever refreshed either — the refresh only covers cards that already have a complete copy. | No card ever gains a `comments_synced_at`. |
| A comment deleted on the host | The stored copy remains. Deletions are not detected: the incremental read asks for what changed, and a deleted comment does not come back to say so. | Nothing. Run `POST /api/cards/import?full=true` if it matters; a full pass still only adds and updates, so removing a deleted comment needs the card re-imported. |
| A comment body containing HTML or a `javascript:` link | It renders as the visible characters it is. The cockpit's Markdown renderer emits React nodes, never `dangerouslySetInnerHTML`, and only allows `http(s)` in an `href`. | The markup shows as text; the link is not clickable. |
| Very large threads (over 5000 comments on one issue) | The read fails rather than storing a truncated thread. A partial thread presented as complete is worse than a visible failure. | `comments.ok: false` with a "narrow the since window" reason. |

### Recommendation

Leave `CFACTORY_IMPORT_COMMENTS=true`. On GitHub it costs one extra API call per
repository per cycle regardless of how big the board is, and it is the difference
between planning from what somebody asked for and planning from what the team
actually decided.

On GitLab or Azure DevOps, weigh it against your backlog size: a refresh there is
one call per card per cycle. Raise `CFACTORY_IMPORT_POLL_SECONDS` before you turn
comments off — a ten-minute cycle on a 100-card GitLab repository is 600 calls an
hour, comfortably inside GitLab's limits, and you keep the discussion.

Do not lower `CFACTORY_IMPORT_COMMENT_BACKFILL_MAX` to smooth a cold start you
only pay once. Raise it instead if you would rather a newly connected repository
were comment-complete in one pass and you know the host will take the burst.

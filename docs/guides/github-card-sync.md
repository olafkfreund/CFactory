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

## Enabling it

```bash
CFACTORY_GITHUB_TOKEN=<a token that can open issues>   # required; unset = OFF
CFACTORY_GITHUB_REPO=owner/repo                        # optional; where new issues are opened
CFACTORY_GITHUB_API_URL=https://api.github.com         # optional; GitHub Enterprise
```

The bare `GITHUB_TOKEN` / `GH_TOKEN` are deliberately **not** read. This
credential files issues in a real repository, and an ambient `gh` login on a
developer's machine must not be able to switch that on by accident.

With a token but no repo, cards can only *adopt* existing issues — nothing is
ever created.

## Other git hosts: GitLab and Azure DevOps (RFC-0020 phase 1)

GitHub is the default, and a deploy that never sets `CFACTORY_GIT_PROVIDER`
behaves exactly as this guide describes. Point the board at another host with:

```bash
CFACTORY_GIT_PROVIDER=gitlab                           # github (default) | gitlab | azure_devops
CFACTORY_GIT_PROVIDER_TOKEN=<a token that can open issues>
CFACTORY_GIT_PROVIDER_URL=https://gitlab.example.com   # optional; self-hosted instance
CFACTORY_GITHUB_REPO=group/subgroup/project            # the host's project path
```

`CFACTORY_GITHUB_REPO` keeps its name (renaming a setting breaks running deploys
for nothing) but is read as *the provider's project path*: `owner/repo` on
GitHub, `group/project` on GitLab, `organization/project/repo` on Azure DevOps.
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
| A card write that leaves it `ready` (either surface) | Opens an issue in `CFACTORY_GITHUB_REPO`, or adopts the one named by `issue_ref`, then mirrors it down. |
| A card write on a card that already has an `issue_ref` | Mirrors the issue down. |
| `POST /api/cards/{card_key}/sync-github` | On-demand sync. |
| MCP `cfactory_sync_card_github` | The same operation, same code (RFC-0019 §3.3). |

**Adopting an existing issue:** set the card's `issue_ref` to `owner/repo#123`
(over `PATCH` or the MCP tools). The card adopts that issue — no new issue is
opened, and no duplicate is possible.

**Idempotency:** `issue_ref` non-NULL *is* "this card already has an issue",
exactly as a non-NULL `correlation_key` means "already in the factory". Syncing
twice adopts and mirrors; it never opens a second issue.

**Issues are opened without labels.** The fleet's issue-driven intake (RFC-0011)
triggers on a `factory:<tier>` label, and a `ready` card has already been
dispatched into the factory by the board's own intake path — labelling the issue
would build the same card twice.

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

### It is a poll. It is not live.

There is still no webhook receiver, so:

- the first import backfills every open issue (up to `CFACTORY_IMPORT_MAX` —
  truncation is reported, never silent);
- after that a `last_synced_at` watermark exists, and each run asks only for
  issues updated since it, minus a 60-second overlap for clock skew, with the
  state widened to `all` so closures and reopenings are caught;
- set `CFACTORY_IMPORT_POLL=true` to have that run every
  `CFACTORY_IMPORT_POLL_SECONDS` (default 300) in the background.

An issue filed a second ago is on the board somewhere between zero and one poll
interval later — never instantly. The import summary shows `last_synced_at` for
exactly that reason.

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

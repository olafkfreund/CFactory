---
status: draft
issue: 561
author: olafkfreund
---

# Intent: an `investigate` card action for read-only investigations

Needed by olafkfreund/RFactory#18, whose plan is approved. It depends on olafkfreund/AIFactory#2156, the `investigate` run kind (PR olafkfreund/AIFactory#2168, off by default). Its network pieces are already in production (factory-gitops#412).

## Problem

In a call, RFactory can only talk. #18 lets a meeting approve a **read-only investigation**: Claude reads a repository at a pinned commit, may run the project's tests in an isolated pod, and reports back. AIFactory now has the run kind. RFactory, however, reaches the factories only through CFactory, and CFactory has no way to dispatch one:

- **No action.** Cards dispatch `plan`, `code`, `test` and `run`, and nothing else.
- **No safe card to hang it on.** A card with no tier is idle only until something gives it work. Every one of these paths can turn it into ordinary work or publish it:
  - **Intake:** `card_intake.maybe_dispatch` builds a `ready` card once it has a tier.
  - **Stage actions:** `actions/plan|code|test` call `dispatch_stage`. `actions/run` writes queued stages into `stage_runs` *before* dispatching (`card_intake.start_sequence`).
  - **Completion events:** `apply_status` rewrites the status and starts the next queued stage of any card whose `correlation_key` matches.
  - **GitHub:**
    - `maybe_sync` opens an issue when a card reaches `ready`, and resyncs on any write once `issue_ref` is set;
    - `POST /sync-github` calls `sync_card` directly, bypassing `maybe_sync`.

    Either would publish what was said in the meeting.
- **No commit or test command to pin.** CFactory has no way to read a repository's default-branch commit, and nothing in it records a test command. AIFactory's design takes the command from CFactory, and RFactory's approval text shows the commit and the command to the meeting. Both must come from CFactory, never from speech.
- **No way to follow a run.** RFactory needs to read the run's state, logs and report, renew its lease every 10 s, and stop it. AIFactory exposes these per run (`/api/investigations/{id}`, `/logs?after=`, `/lease`, `/stop`). CFactory relays none of them.
- **CFactory is the only scope boundary.** CFactory calls AIFactory with its own credential, which is a member of the intake project. AIFactory cannot tell one CFactory tenant or key from another, so CFactory alone must stop:
  - a read key from starting or extending a run;
  - one tenant from touching another tenant's run.

## Proposed outcome

With the switch on, RFactory can do all of this through CFactory. Every operation is on both REST and MCP, writes need the write key, and reads need the read key:

1. **Create an investigation card** for one repository. The card can never start plan, code or test work, and never opens, adopts or syncs a GitHub issue.
2. **Read, for that repository:**
   - its default-branch commit;
   - its configured test command, or "none configured" (`null`).
3. **Dispatch the investigation:** `request`, `sha`, the approved `test_command` (a string or `null`) and `limits`. Before creating the run, the dispatch call checks again itself, without trusting what was read at proposal time:
   - that the repository, and its AIFactory project, are still the card's;
   - that `sha` is on the repository's default branch;
   - that the approved command equals the configured one. A `null` matches only "none configured", which starts an investigation with no tests.

   If any check fails, nothing starts. Otherwise CFactory creates the AIFactory run, records stage `investigate` with the run id on the card, and returns `{ run_id }`.
4. **Renew the lease and stop the run.** CFactory relays both after checking that the run belongs to the caller's tenant and card.
5. **Read the run** after the same ownership check:
   - its state, including whether its pods are gone;
   - its logs, by cursor;
   - its report.
6. **Recover by card.** If RFactory lost the start response, it can still find, stop and follow the run through the card key, even after the card is deleted, until its pods are confirmed gone.

With the switch off, none of these exist, and nothing else in CFactory changes. Cards already created stay inert.

## Affected users and systems

- **CFactory:**
  - the card model and every write path above: intake, stage actions, sequences, completion events, GitHub sync, create and update;
  - `routes_cards.py` and the MCP tools (`mcp.py`);
  - the AIFactory client;
  - the git providers, for a default-branch lookup;
  - repository configuration, which gains a test command;
  - `openapi.yaml`;
  - the planning-card schema check (`scripts/check_planning_card_conformance.py`, Factory#554), if the card gains a field;
  - the Helm chart and the factory-gitops manifests for the switch.
- **AIFactory:** called through its investigation API, not changed. Creating a run needs `member` on the run's project, so CFactory's AIFactory credential must be a member of each repository's intake project.
- **RFactory:** the consumer. Its plan step 3 records any difference from the shapes defined here as a deviation. One is already known: it expects `pod_gone`, and AIFactory returns `pods_gone`.
- **People:** whoever sets a repository's test command, since that command runs in a test pod when a meeting asks for tests.

## Constraints

- **The switch:** off by default; with it off, CFactory behaves exactly as today.
- **The investigation card is inert, on every path:**
  - it is an investigation for its whole life, and no update turns it into an ordinary card or back;
  - it never gets a tier, `issue_ref` or `correlation_key`;
  - intake, the stage actions, `run` and completion events refuse it before writing anything;
  - `sync-github` refuses it before any provider call;
  - `investigate` is refused on an ordinary card.
- **Configuration only:**
  - the commit is checked against the default branch;
  - the test command comes from repository configuration, never from the request;
  - setting or changing that command needs the write key and is audited;
  - any mismatch refuses the dispatch, with no fallback.
- **Checked at dispatch:** the checks run inside the dispatch call, immediately before the run is created, and refuse if anything changed since approval. A configuration change after the meeting's approval starts nothing. (These are calls to the git provider and to AIFactory, so they can't be one transaction. "One step" means one call that trusts nothing read earlier.)
- **Key scopes:**
  - creating a card, dispatching, renewing the lease and stopping need the write key, on REST and MCP alike;
  - a read key can never start or extend a run;
  - these writes are refused when no keys are configured, even in dev-open mode;
  - all four are audited.
- **Ownership:** every relayed call resolves the run through the caller's tenant and card before using CFactory's AIFactory credential. A run id alone is never enough.
- **Limits:** passed to AIFactory, which bounds them per kind. CFactory may refuse anything above AIFactory's caps but never raises them.
- **No secrets:** none in card fields, the audit log or relayed logs. AIFactory scrubs logs before storing them. What the meeting said (the request, the card title) is stored on the card and in the audit log and goes nowhere else.
- **Other cards unchanged:** the existing actions, tier routing and GitHub sync for every other card behave as today.
- **Workflow:** the artifact workflow applies, and agents that are started never stage, commit, push or deploy.

## Open questions

1. **Where the test command lives.** Options:
   - per git repository, next to `intake_project`;
   - in a new project setting.

   *Recommendation:* per repository, because the investigation is already pinned to one repository.
2. **How the card is made inert.** Options:
   - a card field, for example `kind: investigation`, that every write path checks;
   - a separate investigation record that is not a card.

   *Recommendation:* a card field. #18 already plans "Investigate: <request>" cards, so they show on the board like any other card. It is set at creation, never changes, and every path above refuses it (an allow-list, not a growing deny-list). The cost is updating the planning-card schema check.
3. **"On the default branch".** Options:
   - the branch's current head;
   - any commit reachable from it.

   *Recommendation:* reachable from it, read through the repository's git provider. A commit that lands between the proposal and the approval then does not refuse a run of the approved commit.
4. **What the board shows:** the run's state and the report summary, or only the run id and state, with logs and the report on RFactory's page? *Recommendation:* run id and state only for now.
5. **The read side.** Options:
   - relay AIFactory's responses as they are;
   - define CFactory's own shapes.

   *Recommendation:* relay them as they are, field for field, minus anything internal. RFactory's plan then records its one rename (`pod_gone` → `pods_gone`) as a deviation.
6. **The switch.** Should `CFACTORY_INVESTIGATIONS=off|on` also have AIFactory's `drain`? *Recommendation:* yes.
   - `drain` refuses new investigation cards and new dispatches.
   - It removes `cfactory_investigate_card` from the MCP tool list, so RFactory's `investigateAvailable()` turns false.
   - It keeps lease, stop, recovery and reads.

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
- **No safe card to hang it on.** A card with no tier is idle only until someone gives it a tier. Then CFactory's intake hook (`card_intake.maybe_dispatch`) builds it as ordinary work, and the explicit stage actions work on it too. A `ready` card also opens a GitHub issue by itself (`github_sync.maybe_sync`), which would publish what was said in the meeting.
- **No test command.** Nothing in CFactory records a project's test command. AIFactory's design takes the command from CFactory, and RFactory's approval text shows it to the meeting. It has to come from configuration, never from speech.
- **No way to follow a run.** RFactory needs to read the run's state, logs and report, renew its lease every 10 s, and stop it. AIFactory exposes these per run (`/api/investigations/{id}`, `/logs?after=`, `/lease`, `/stop`). CFactory relays none of them.

## Proposed outcome

With the switch on, RFactory can do all of this through CFactory, with the write key for writes and the read key for reads:

1. **Create an investigation card** that can never start plan, code or test work, and never opens or syncs a GitHub issue.
2. **Read the project's configured test command** (or "none configured") and the default-branch commit, to show in the approval text.
3. **Dispatch the investigation:** `request`, `sha` and the approved `test_command`. CFactory checks, in one step with the run's creation:
   - that `sha` is on the repository's default branch;
   - that the approved test command equals the configured one at that moment.

   If either fails, nothing starts. Otherwise CFactory creates the AIFactory run, records stage `investigate` with the run id on the card, and returns `{ run_id }`.
4. **Renew the lease and stop the run**, both relayed to AIFactory.
5. **Read the run's state** (including `pods_gone`), its logs by cursor, and its report.

With the switch off, the action, the relays and the MCP tool do not exist, and nothing else in CFactory changes.

## Affected users and systems

- **CFactory:**
  - the card model and its write paths (intake hook, stage actions, GitHub sync);
  - `routes_cards.py` and the MCP tools (`mcp.py`);
  - the AIFactory client;
  - repository or project configuration, which gains a test command;
  - `openapi.yaml`;
  - the Helm chart and the factory-gitops manifests for the switch.
- **AIFactory:** called through its investigation API, not changed. AIFactory authorizes each call against the run's project, and creating a run needs `member`. CFactory's AIFactory credential must therefore be a member of the repository's intake project.
- **RFactory:** the consumer. Its plan step 3 records any difference from the shapes defined here as a deviation.
- **People:** whoever sets a project's test command, since that command runs in a test pod when a meeting asks for tests.

## Constraints

- **Off by default** behind a switch; with it off, CFactory behaves exactly as today.
- **The investigation card is inert:**
  - setting a tier on it, or moving it to `ready`, must never dispatch it;
  - the stage actions refuse it;
  - it never creates, adopts or syncs a GitHub issue.
- **Configuration only:** the test command comes from CFactory configuration, never from the request. A mismatch with the approved command refuses the dispatch, with no fallback.
- **One check, one creation:** the commit check, the command check and the run's creation happen in one step. A configuration change between the approval and the dispatch starts nothing.
- **Key scopes:** writes (create, dispatch, lease, stop) need the write key; reads need the read key. A read key can never start or extend a run.
- **The caller's limits** are passed to AIFactory, which bounds them per kind. CFactory may refuse anything above AIFactory's caps but must never raise them.
- **No secrets** in card fields, the audit log or relayed logs. AIFactory scrubs logs before storing them; CFactory relays them as they are.
- **The existing actions** (`plan`, `code`, `test`, `run`), tier routing and GitHub sync for every other card are unchanged.
- **Workflow:** the artifact workflow applies, and agents that are started never stage, commit, push or deploy.

## Open questions

1. **Where the test command lives.** Options:
   - per git repository, next to `intake_project`;
   - in a new project setting.

   *Recommendation:* per repository, because the investigation is already scoped to one repository.
2. **How the card is made inert:**
   - a new card field (for example `kind: investigation`) that every write path respects;
   - a separate investigation record that is not a card.

   *Recommendation:* a card field. #18 already plans "Investigate: <request>" cards, and the board then shows them like any other card.
3. **Showing the run on the board.** Should the card show the run's state and the report summary, or only the run id and state, leaving logs and the report to RFactory's page? *Recommendation:* state and run id only for now.
4. **The read side:** relay AIFactory's responses as they are, or define CFactory's own shapes? *Recommendation:* relay as they are, with `pods_gone` and the cursor unchanged, so RFactory's plan step 3 maps one to one.
5. **The switch name:** for example `CFACTORY_INVESTIGATIONS=off|on`. Should it also have AIFactory's `drain` (refuse new dispatches, keep stop, lease and reads)? *Recommendation:* yes, for the same rollback reason as AIFactory's deviation 12.

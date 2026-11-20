---
status: draft
issue: 561
intent: intent/2026-11-20-561-investigate-card-action.md
---

# Spec: an `investigate` card action for read-only investigations

`B` means `apps/backend/cfactory/`. AIFactory's investigation API is on PR olafkfreund/AIFactory#2168. The design reads it as it is there and does not change it:
- `POST /api/investigations {project_id, sha, request, test_command, limits}` → 200 `{id, state}`;
- `GET /{id}` → `{id, state, reason, pods_gone, tests, report, usage: {tokens, turns, run_tests}}`;
- `GET /{id}/logs?after>=0&1<=limit<=500` (defaults 0 and 100) → `{entries: [{seq, at, line}], cursor}`;
- `POST /{id}/lease` → `{ok: true}`;
- `POST /{id}/stop` → `{state, pods_gone}`.

AIFactory's final states are `completed`, `failed`, `stopped` and `expired`.

## Design

### 1. The switch, and where it applies

- **`Settings.investigations: Literal["off", "on", "drain"] = "off"`** (`B/config.py`, env `CFACTORY_INVESTIGATIONS`), next to `jira_enabled` (`B/config.py:217`). Any other value fails at startup.
- **Single-tenant only.** With `multi_tenant` on (`B/config.py:317`), every investigation route and tool refuses with 409 `multi_tenant_unsupported`. CFactory takes the tenant from the `X-Tenant-Id` header and does not bind it to the key (`B/config.py:489-499`), so no per-tenant ownership check could hold. Production runs single-tenant (`multi_tenant` defaults to false, and factory-gitops does not set it). Lifting this needs keys bound to tenants, its own issue.
- **`off`:**
  - no investigation route is mounted;
  - no investigation tool is listed, and a direct `tools/call` of one returns "unknown tool";
  - §3's guards still apply, so existing investigation cards stay inert.
- **`drain`:**
  - creating an investigation card and `actions/investigate` return 503 `investigations_draining`, on REST and on direct MCP `tools/call`;
  - both tools are also left out of `tools/list`, so RFactory's `investigateAvailable()` turns false;
  - lease, stop, recovery and reads keep working.
- **Rollback** is `on` → `drain` → off, as in AIFactory's deviation 12. Before switching off, every card with a recorded `run_id` must read `pods_gone: true`. AIFactory must also show no live run for each investigated project, because a start with no recorded id (§5) is visible only there.

### 2. The investigation card

- **The column:** `CardRow.kind: String(16), NOT NULL, server_default "work"`, with values `work` and `investigation` (`B/cards.py:170-264`). It needs an Alembic revision after head `7e4c2a91d6b8` plus a `_LATE_COLUMNS` entry (`B/cards.py:622-637`). `tests/test_schema_bootstrap.py:108` keeps the two in step.
- **`kind` is not a field of `Card`, `CardCreate` or `CardUpdate`** (`B/cards.py:336-498`). The external planning-card gate compares their field names with the hub contract, and `extra="forbid"` makes a new field visible to it.
  - Instead, `Card` gets a Pydantic `PrivateAttr` `_kind = "work"`. It is not serialized and not in the JSON schema. The store sets it whenever it builds a `Card` from a row (`get`, `get_by_correlation_key`, `list`, `create`, `update`).
  - `card_intake.is_investigation(card)` reads `_kind`.
  - Because the generic API cannot express `kind`, no generic write can create an investigation card or change a card's kind. Only §4's create route sets it, and nothing unsets it.
- **What the card holds:**
  - `title`: `Investigate: ` followed by the first 200 characters of the request (plus `…` when cut), within `CardRow.title`'s 512 (`B/cards.py:180`);
  - `description`: the full request, up to 2,000 characters, which is AIFactory's limit;
  - `tier`, `issue_ref` and `correlation_key`: `None`;
  - `status`: `backlog`;
  - `repository_id`: the investigated repository, given explicitly and never filled from the tenant default.
- **`stage_runs.investigate`**, already part of `Card`, so the board shows it:

  ```json
  {"service": "aifactory", "status": "<state>",
   "detail": {"binding": {"repository_id": 3, "repo": "owner/name", "aifactory_project_id": "p1"},
              "attempt": "<uuid>", "run_id": "<id>" | null, "sha": "…", "pods_gone": false}}
  ```

  `binding` is fixed when the card is created. The board shows the state and the run id only (intent decision 4).

### 3. Guards: an investigation card refuses every ordinary path

Each guard is the path's first statement after the card is loaded, before any project resolution, write or provider call. Where a path did not load the card, it now loads it.

| Path | Guard | Result |
|---|---|---|
| `card_ops.update_card` (update, move, reprioritise; REST `B/routes_cards.py:242`; MCP `B/mcp.py:1174`, 1378-1379) | load the card, then guard, before `store.update` (`B/card_ops.py:362`) | `InvestigationCardError` |
| `card_ops.sync_card_github` (`POST /sync-github`, `B/routes_cards.py:277`) | after `get_card` (`B/card_ops.py:389`), before `sync_card` (390) | `InvestigationCardError` |
| `github_sync.sync_card` | first line, before `git_target_for_card` (`B/github_sync.py:302`) | `{ok: false, skipped: "investigation_card"}` |
| `github_sync.maybe_sync` | first line (`B/github_sync.py:384`), before `git_target_for_card` (385) | `None` |
| `card_intake.maybe_dispatch` | first line (`B/card_intake.py:663`) | `None` |
| `card_intake.dispatch_card` (it skips `refuse_reason`) | first line (`B/card_intake.py:620`) | refusal `investigation_card`, with no write |
| `card_intake.dispatch_stage` (`actions/plan|code|test`) | first line (`B/card_intake.py:682`), before project resolution (692) and the completed-stage return | `InvestigationCardError` |
| `card_intake.start_sequence` (`actions/run`) | first line (`B/card_intake.py:728`), before the no-tier refusal (729) and `_write_runs` (750) | `InvestigationCardError` |
| `card_intake.apply_status` | after `get_by_correlation_key` (`B/card_intake.py:856`) | ignored. Unreachable, since the card has no `correlation_key`, but kept |
| `actions/investigate` on a `work` card | §5 check 3 | 409 `not_investigation_card` |

- **`InvestigationCardError`** maps to 409 `investigation_card` in every REST route that can raise it: `update_card`, `sync_card_github` and `_stage_action`. MCP returns the same code as a tool error.
- **Delete stays allowed.** It is a soft delete (`B/cards.py:920`). `CardStore.get` and `_get_row` gain `include_deleted: bool = False` (`B/cards.py:794-822`), and §5 and §6 use it, so a run stays stoppable and readable after its card is deleted (intent outcome 6).
- **`issue_import`** creates and matches cards only by `issue_ref`, which an investigation card never has, so it needs no guard. A test asserts it never touches one.

### 4. Closing the generic executor

`/api/actions/execute` (`B/routes_actions.py:53`) runs a caller-supplied `PreparedAction`, a free root-relative `endpoint` plus `follow_ups`, with CFactory's service credential (`B/actions.py:104-130`, 421-481). Today any write key can therefore reach AIFactory's investigation API directly, past every check here.

- **`execute_action` refuses any step**, primary or follow-up, whose endpoint path starts with `/api/investigations` once normalized:
  - percent-decoded;
  - dot segments resolved;
  - duplicate slashes collapsed;
  - lowercased.

  Such a step returns `{ok: false, status_code: 403, reason: "investigations_via_executor"}` and is audited.
- **The rule applies in every switch state**, because it closes an existing gap.
- **The investigation client in §9** does not use `execute_action`.

### 5. The repository's test command, the proposal read, and card creation

- **The column:** `GitRepositoryRow.investigate_test_command: Text, nullable` (`B/git_connections.py:137-180`), with an Alembic revision and a `_LATE_REPOSITORY_COLUMNS` entry (`B/cards.py:644`).
  - It is exposed on `GitRepository` and editable through `GitRepositoryUpdate`, over REST (`PATCH …/git-repositories/{id}`) and MCP (`cfactory_update_git_repository`). Both already need the write scope (`B/mcp.py:996`).
  - `""` is stored as `null`, meaning "none configured". At most 1,000 characters, with no newline or NUL.
  - A change is audited as `set_investigate_test_command`, with the SHA-256 of the old and new values, never the command text.
- **`GET /api/investigations/context?repository_id=<id>`** needs the read scope. MCP: `cfactory_investigation_context`. It returns:

  ```json
  {"repository_id": 3, "repo": "owner/name", "default_branch": "main", "sha": "<40 hex>", "test_command": "npm test" | null}
  ```

  - With no `repository_id` it uses the tenant's default repository, and the response names the one it used.
  - Refusals:
    - 404 `no_repository` when there is no repository, or the id is unknown in the tenant;
    - 409 `no_aifactory_project` when the repository has no `aifactory_project_id` (`B/git_connections.py:155`);
    - 409 `unsupported_provider` when its provider is not GitHub;
    - 409 `empty_repository` when it has no commits;
    - 502 `provider_unavailable` on a provider error.
- **`POST /api/investigations/cards {request, repository_id}`** needs the write scope. MCP: `cfactory_create_investigation_card`.
  - The repository must exist in the tenant, with an `aifactory_project_id`, on GitHub, using the same refusals as above.
  - `request` is 1 to 2,000 characters. It is refused with 422 `request_contains_secret` when it matches a credential pattern: `ghp_`, `gho_`, `github_pat_`, `sk-`, `AKIA[0-9A-Z]{16}`, `-----BEGIN`, or a `password=`/`token=` assignment. Meeting speech should never carry these.
  - It creates the §2 card with its `binding`, returns the `Card`, and is audited as `create_investigation_card` with the SHA-256 of the request, not its text.

### 6. `POST /api/cards/{key}/actions/investigate`

The body is `{request, sha, test_command, limits}`, and the route needs the write scope. MCP: `cfactory_investigate_card`. Every check runs inside this call, in this order, and trusts nothing read earlier.

1. **The switch is `on`** (`drain` gives 503), the deployment is single-tenant, and the caller passes §8's credential rule.
2. **Inputs:**
   - `sha` is 40 lowercase hex characters;
   - `request` equals the card's `description` exactly;
   - `test_command` is a string or `null`;
   - `limits` has only the keys `minutes`, `tokens`, `turns` and `test_calls`, each a positive integer at most AIFactory's caps (30, 2,000,000, 200 and 10).

   CFactory never raises a limit, and omitted keys are left to AIFactory.
3. **The card:** it is found in the tenant, it is not deleted, and it is an investigation card (otherwise 409 `not_investigation_card`).
4. **No live run.** A recorded run is live when its state is `starting`, `running` or `stopping`, or when it is final with `pods_gone` not yet true. A live run gives 409 `run_live`. `refused` and `uncertain` have no `run_id`, so CFactory does not treat them as live. AIFactory's own per-project slot refuses a second run while one exists there.
5. **The repository and the command:**
   - the binding's repository is re-read by id, with no fallback (a missing row gives 409 `repository_gone`);
   - it must still be GitHub, still name the same `repo`, and still have the same `aifactory_project_id` (otherwise 409 `repository_changed`);
   - the approved `test_command` must equal the repository's `investigate_test_command`, both normalized from `""` to `null`, where `null` matches only `null` (otherwise 409 `test_command_changed`).
6. **The commit** is checked through GitHub's `GET /repos/{repo}/compare/{default_branch}...{sha}`:
   - `identical` or `behind` passes;
   - `ahead`, `diverged`, 404 and 422 give 409 `sha_not_on_default_branch`;
   - any other error gives 502 `provider_unavailable`.

   Nothing starts unless the commit passes.
7. **Reservation**, in one transaction, using the store's existing lock pattern (a no-op `UPDATE` on the card row, then a re-read, as in `_lock_connections`, `B/cards.py:1233-1245`). In it CFactory:
   - re-reads the card (same tenant, investigation, not deleted) and re-checks check 4;
   - re-reads the repository and re-checks check 5, so a change made during the compare call is caught;
   - writes `stage_runs.investigate = {status: "starting", detail: {binding, attempt: <new uuid>, sha, run_id: null, pods_gone: false}}`.

   If any re-check fails, the transaction writes nothing and returns that check's refusal. Of two concurrent dispatches, exactly one reserves.
8. **The run:** `POST {aifactory}/api/investigations {project_id: binding.aifactory_project_id, sha, request, test_command, limits}` is sent with a 60 s timeout. AIFactory's dispatch exports the source and creates the Job and the Secret before it answers.
9. **The result is recorded** in a second locked transaction. It reads deleted cards too, and writes only if `detail.attempt` is still this attempt:

   | AIFactory answer | Recorded | Returned |
   |---|---|---|
   | 200 with a string `id` | `running`, `run_id = id` | `{run_id}` |
   | 400, 403, 404, 409 or 422 | `refused`, with AIFactory's `detail` | 409 with that detail |
   | 503 | `refused`, `investigations_draining` | 503 |
   | Any other status, a malformed 200, a timeout or a connection error | `uncertain`, `run_id: null` | 504 `start_uncertain` |

   Notes on each outcome:
   - **`refused`:** AIFactory may have inserted a row and begun cleanup before refusing (`export failed`, `dispatch failed`). Its slot holds until its pods are gone, so a retry is refused there until then.
   - **`uncertain`:** AIFactory may have started the run. Nobody renews its lease, so AIFactory's watchdog ends it within about 60 s of Job creation, and its slot blocks a second run until its pods are gone. CFactory cannot look the run up, because AIFactory has no lookup by attempt or project. §1's rollback step covers it.
10. **Audit:** `investigate` is recorded with the attempt, `ok`, the status code and the run id.

The checks happen inside one call, not as one transaction, because the provider and AIFactory are remote. The re-check in check 7 shrinks the window to the time between the reservation and AIFactory's own create.

### 7. The relays: lease, stop, reads and recovery

These are all under `/api/cards/{key}/investigate/{run_id}`, plus one recovery route:

| Route | Scope | Upstream | Timeout | Audit |
|---|---|---|---|---|
| `POST …/lease` | write | `POST /{id}/lease` | 10 s | every call |
| `POST …/stop` | write | `POST /{id}/stop` | 45 s | every call |
| `GET …` | read | `GET /{id}` | 10 s | none |
| `GET …/logs?after=&limit=` | read | `GET /{id}/logs`, after `≥ 0`, limit 1 to 500 | 10 s | none |
| `GET /api/cards/{key}/investigate` (recovery) | read | none: returns `stage_runs.investigate` | | none |

MCP: `cfactory_investigation_lease`, `_stop`, `_get`, `_logs` and `_recover`.

- **Ownership.** Each relay first:
  - loads the card in the tenant, deleted ones included;
  - checks that it is an investigation card;
  - checks that `run_id` equals its recorded `run_id`.

  Otherwise it returns 404. Only an id that passed this check reaches an upstream URL. AIFactory then checks CFactory's credential against the run's project.
- **Recording what AIFactory reports.** After `GET …` and `stop`, CFactory records AIFactory's `state` and `pods_gone` on the card, in a locked write that:
  - applies only when the recorded `run_id` still equals this one, so a slow read of an earlier run cannot overwrite a newer reservation;
  - is monotonic: `pods_gone` never goes from true to false, and a final state never goes back to a live one.
- **An ambiguous stop** (a timeout or 5xx) records `stopping` and returns 504 `stop_uncertain`. The caller follows with `GET …` until `pods_gone`.
- **Responses are relayed field for field** (intent decision 5). RFactory's plan records the `pod_gone`/`pods_gone` rename and any other difference as a deviation.

### 8. Credentials for investigation writes

Card create, dispatch, lease and stop need the write scope **and** a configured credential.

- **REST:** `require_scope("write")` (`B/auth.py:262`), and also 403 `keys_required` when the key store is open (dev-open, `B/auth.py:144`).
- **MCP:** `_authenticate` (`B/mcp.py:1009-1057`) reports how it granted access:
  - investigation writes accept a matched scoped key with `write`, or the legacy `mcp_secret`, which is a configured credential;
  - they refuse with `keys_required` when access came from `mcp_dev_open` (`B/config.py:288`).

  So neither REST's nor MCP's dev-open setting can start or extend a run.
- **Reads** need the read scope, and are allowed under dev-open as other reads are.

### 9. Code layout

- **`B/investigations.py` (new):** the §5–§7 logic, shared by `B/routes_investigations.py` (new) and the MCP handlers. It has its own small HTTP helper, with per-call timeouts, built on:
  - `settings.aifactory_api_url`;
  - `settings.token_for("aifactory")`;
  - the transport seam (`action_transport_dep`, `B/api_deps.py:104-107`), so tests use `tests/cards_harness.py`'s `Upstream`.
- **`B/git_providers.py`** gains, on `HttpGitHubProvider` only:
  - `default_branch_head(repo) -> (branch, sha)`: `GET /repos/{repo}` for `default_branch`, then `GET /repos/{repo}/commits/{quote(branch, safe="")}` for its sha. An empty repository raises `EmptyRepository`.
  - `is_reachable_from_default(repo, branch, sha) -> bool`: the check 6 compare.
- **Other providers** do not implement these, and the §5 and §6 checks refuse them first.

### 10. Docs and deploy

- **`openapi.yaml`** (hand-maintained) gains the new routes and `investigate_test_command`. The Card schemas stay as they are.
- **The env table** (`techdocs/dependencies.md:93-102`) and `.env.example` get `CFACTORY_INVESTIGATIONS`.
- **Helm:** `charts/cfactory` gets an `investigations` value mapped in `templates/configmap.yaml`.
- **factory-gitops** is left as it is. Turning the switch on belongs to RFactory#18's rollout.

## Alternatives rejected

- **A no-tier card with no new field.** A later tier, `ready` or sync makes it real work or publishes it (intent, Problem).
- **`kind` as a public field of `Card`, `CardCreate` or `CardUpdate`.** It changes the contract the external gate checks, and a generic update could flip it. A private attribute gives the guards the value without either problem.
- **A separate investigations table.** The intent chose a card (decision 2) so the board shows it. A separate table would need its own tenancy and soft delete.
- **A tenant check on a header the caller chooses.** It isolates nothing. Single-tenant only, until keys are bound to tenants.
- **Leaving `/api/actions/execute` as it is.** Every check here could be bypassed through it.
- **A conditional `UPDATE … WHERE stage_runs …`.** `stage_runs` is plain `JSON`, which PostgreSQL cannot compare with `=`, and JSON paths differ between SQLite and PostgreSQL. The store's existing lock-and-re-read pattern works on both.
- **The shared 10 s action timeout for dispatch and stop.** AIFactory's dispatch exports and creates resources, and its stop waits for pods, so 10 s would turn normal calls into false `uncertain` results.
- **Taking the test command from the request**, or **head-only commit checks.** The intent rules out both (decision 3 and its constraints).
- **Adding an AIFactory lookup by project now**, to recover an `uncertain` start. That is an AIFactory API change on an open PR. Its slot and lease already make an unrecorded run end and block reuse until it is gone. Left as a follow-up.
- **GitLab, Azure DevOps and Bitbucket now.** Their providers have no reachability check. Fail closed until one is added and tested.

## Risks

- **An unrecorded run.** After an `uncertain` start, a run CFactory cannot name may exist for about 60 s. AIFactory ends it, and its slot refuses a second one meanwhile. Rollback must check AIFactory directly (§1).
- **The external conformance gate.** If the hub contract enumerates `stage_runs` keys, `investigate` could fail it. The plan checks this against the hub script first; if it fails, the run moves to its own column.
- **`PrivateAttr` must be set on every path that builds a `Card`.** A path that forgets it reports `work` and lets an investigation card through. Mitigation:
  - one constructor helper in the store builds every `Card` and sets it;
  - a test builds a card through each store method and asserts `_kind`;
  - §3's guards are each tested on a card read through the path's own loader.
- **A stale `pods_gone`.** The card learns it only on a read or a stop, so a stale `false` refuses a new run (it fails closed) until a read.
- **GitHub rate limits:** about three calls per dispatch and two per context read, which is fine at meeting scale.
- **The credential patterns** catch common token shapes, not every secret a person might say. AIFactory scrubs its logs, and the request reaches the card and AIFactory only.

## Verification

- **`tests/test_investigations.py` (new)**, using `Upstream` and a fake GitHub transport:
  - each §6 refusal, one test per check, with nothing sent to AIFactory;
  - commands: `null` vs configured, configured vs `null`, and `null` vs `null` (which dispatches with no tests);
  - compare results: `identical`, `behind`, `ahead`, `diverged`, 404, 422 and 500;
  - a repository changed during the compare call: refused at reservation, nothing sent;
  - two concurrent dispatches: exactly one reaches AIFactory;
  - a card deleted during the AIFactory call: the result is still recorded on it;
  - a slow read of run A after run B was reserved: B's record is unchanged;
  - each row of the §6 check 9 table, including a malformed 200 and a 500;
  - relays: a wrong `run_id` or a work card gives 404 with no upstream call; a deleted card's run is still readable and stoppable; `pods_gone` never regresses;
  - a stop timeout gives `stopping`;
  - responses are relayed field for field.
- **Guards:** for every row of the §3 table, the path called on an investigation card writes nothing and calls no provider, and the REST status is 409, not 500. The `run` test checks `stage_runs` is unchanged.
- **The executor:** `/api/actions/execute` refuses `/api/investigations`, `/API/Investigations/x`, `/api/tasks/../investigations`, `/api//investigations` and `%2Fapi%2Finvestigations` as the primary step and as a follow-up, and still runs every existing action.
- **Auth:**
  - a read key gets 403 on all four writes, on REST and MCP;
  - REST dev-open with no keys, and MCP dev-open with no keys, both get `keys_required`;
  - with `multi_tenant` on, every investigation route refuses;
  - `tools/list` and direct `tools/call` behave as §1 says in `off`, `on` and `drain`.
- **Data:** a request with a token pattern is refused; audit rows carry hashes, not request or command text; a long request gives a title of at most 214 characters and the full description.
- **Migrations and models:** `tests/test_schema_bootstrap.py` passes, and a `create_all` store gains both columns. `tests/test_board_parity.py` passes. Every store method sets `_kind`.
- **The full suite and the repository's CI gates**, including planning-card conformance, are green.
- **Live, as RFactory#18's step:** a real dispatch against AIFactory with the switch on, after AIFactory#2168 is released. That check is not part of this change.

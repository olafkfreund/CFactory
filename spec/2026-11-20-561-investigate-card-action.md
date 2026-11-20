---
status: draft
issue: 561
intent: intent/2026-11-20-561-investigate-card-action.md
---

# Spec: an `investigate` card action for read-only investigations

`B` means `apps/backend/cfactory/`. AIFactory's investigation API is on PR olafkfreund/AIFactory#2168. The design reads it as it is there and does not change it.

## Design

### 1. The switch

- **`Settings.investigations: Literal["off", "on", "drain"] = "off"`** (`B/config.py`, env `CFACTORY_INVESTIGATIONS`), next to `jira_enabled` (`B/config.py:217`). Any other value fails at startup.
- **`off`:**
  - no investigation route is mounted;
  - no investigation tool is listed or callable;
  - every guard in §3 still applies, so existing investigation cards stay inert.
- **`drain`:**
  - creating an investigation card and `actions/investigate` return 503 `investigations_draining`;
  - `cfactory_create_investigation_card` and `cfactory_investigate_card` are removed from `tools/list`, so RFactory's `investigateAvailable()` turns false;
  - lease, stop, recovery and every read keep working.
- **Rollback is `on` → `drain` → off.** Switch to off only after every recorded run reads `pods_gone: true`, for the same reason as AIFactory's deviation 12.

### 2. The investigation card

- **The column:** `CardRow.kind: String(16), NOT NULL, default "work"`, with values `work` and `investigation` (`B/cards.py:170-264`). It needs an Alembic revision after head `7e4c2a91d6b8` that adds the column with `server_default="work"`, plus a `_LATE_COLUMNS` entry (`B/cards.py:622-637`) so that `create_all` stores add it too. `tests/test_schema_bootstrap.py:108` keeps the two in step.
- **`kind` stays off the card's API models.** It is not added to `Card`, `CardCreate` or `CardUpdate` (`B/cards.py:336-457`). The external planning-card gate checks those models' field names against the hub contract (`.github/workflows/planning-card-conformance.yml`), and `extra="forbid"` means a new field fails it.

  Because the generic API cannot express `kind`, no generic write can create an investigation card or change a card's kind. Only §4's create route sets it, and nothing ever unsets it.
- **What the board sees:** the investigation appears through the existing `stage_runs` JSON, which is already in `Card`. Its key is `investigate`:

  ```json
  {"service": "aifactory", "status": "<state>", "detail": {"run_id": "…", "repository_id": 3, "sha": "…"}}
  ```

  There are no logs and no report on the board (intent decision 4).
- **The card is created inert:** `tier=None`, `issue_ref=None`, `correlation_key=None`, `status="backlog"`. It has a fixed `repository_id`, the repository the meeting is investigating. That is required, never the tenant default by fallback.

### 3. Guards: an investigation card refuses every ordinary path

One predicate, `is_investigation(card) -> bool` in `B/card_intake.py`. Each guard runs before the path's first write or provider call (the line numbers are the first write each guard must precede):

| Path | Guard placement | Result |
|---|---|---|
| `card_ops.update_card` (update, move, reprioritise; REST `B/routes_cards.py:242`; MCP `B/mcp.py:1174`, 1378-1379) | before `store.update` (`B/card_ops.py:362`) | 409 `investigation_card` |
| `card_ops.sync_card_github` (`POST /sync-github`) | before `sync_card` (`B/card_ops.py:390`) | 409 `investigation_card` |
| `github_sync.sync_card` | first line, before `git_target_for_card` (`B/github_sync.py:302`) | `{ok: false, skipped: "investigation_card"}`. This also covers `maybe_sync` |
| `github_sync.maybe_sync` | first line (`B/github_sync.py:383`) | `None` (not a sync event) |
| `card_intake.maybe_dispatch` | first line (`B/card_intake.py:663`) | `None` |
| `card_intake.dispatch_card` (it skips `refuse_reason`) | first line (`B/card_intake.py:620`) | refusal `investigation_card`, with no write |
| `card_intake.dispatch_stage` / `card_ops.run_card_stage` (`actions/plan|code|test`) | before `refuse_reason` (`B/card_intake.py:693`) | 409 `investigation_card` |
| `card_intake.start_sequence` (`actions/run`) | before `_write_runs` (`B/card_intake.py:750`) | 409 `investigation_card`, with no queued stage written |
| `card_intake.apply_status` | after `get_by_correlation_key` (`B/card_intake.py:856`), before `_settle` | ignored. Unreachable, since the card has no `correlation_key`, but kept |
| `actions/investigate` on a `work` card | §5 check 2 | 409 `not_investigation_card` |

- **Delete stays allowed.** It is a soft delete (`B/cards.py:920`). The investigation routes in §5 find a card's run even after the card is deleted (`get(..., include_deleted=True)`), so a run can always be stopped and followed until its pods are gone (intent outcome 6).
- **`issue_import`** creates cards only from issues and matches them by `issue_ref`, which an investigation card never has, so it needs no guard. A test asserts that an import never touches an investigation card.

### 4. The repository's test command and the proposal read

- **The column:** `GitRepositoryRow.investigate_test_command: Text, nullable` (`B/git_connections.py:137-180`), with an Alembic revision and a `_LATE_COLUMNS`-style bootstrap entry if that table has one.
  - It is exposed on `GitRepository` and editable through `GitRepositoryUpdate` (`PATCH /api/tenants/{tenant}/git-repositories/{id}`, MCP `cfactory_update_git_repository`). Both already need the write scope (`B/mcp.py:996`).
  - `null` or an empty string means "none configured". At most 1,000 characters, with no newline or NUL.
  - A change is audited with kind `set_investigate_test_command`, and the old and new values are hashed, not stored.
- **`GET /api/investigations/context?repository_id=<id>`** needs the read scope. MCP: `cfactory_investigation_context`. It returns:

  ```json
  {"repository_id": 3, "repo": "owner/name", "default_branch": "main", "sha": "<40 hex>", "test_command": "npm test" | null}
  ```

  - With no `repository_id` it uses the tenant's default repository, and the response always names the one it used.
  - `sha` is the default branch's head when this is read.
  - It refuses with 409 when:
    - the repository has no `aifactory_project_id` (`B/git_connections.py:155`): `no_aifactory_project`;
    - its provider is not GitHub: `unsupported_provider`. Fail closed: only GitHub is supported for now.
- **`POST /api/investigations/cards {request, repository_id}`** needs the write scope. MCP: `cfactory_create_investigation_card`.
  - It creates the §2 card titled `Investigate: <request>`. `request` is 1 to 2,000 characters.
  - It returns the `Card`.
  - It is audited as `create_investigation_card`.

### 5. `POST /api/cards/{key}/actions/investigate`

The body is `{request, sha, test_command, limits}`, and the route needs the write scope. MCP: `cfactory_investigate_card`. Every check runs inside this call, in this order, and trusts nothing read earlier:

1. **The switch is `on`.** `drain` gives 503.
2. **The card:** it is found in the caller's tenant, it is an investigation card (otherwise `not_investigation_card`), and it is not deleted.
3. **No live run:** the card's `stage_runs.investigate` is absent, or its last read state is final and `pods_gone`. Otherwise 409 `run_live`.
4. **Inputs:**
   - `sha` is 40 lowercase hex characters;
   - `request` equals the request the card was created with;
   - `limits` has only the keys `minutes`, `tokens`, `turns` and `test_calls`, each a positive integer at most AIFactory's caps (30, 2,000,000, 200 and 10).

   CFactory never raises a limit. Omitted keys are left to AIFactory's defaults.
5. **The repository** is re-read now: the card's `repository_id` must still exist, still have an `aifactory_project_id`, and still be GitHub.
6. **The test command:** the approved `test_command` equals the repository's `investigate_test_command`, compared after normalizing `""` to `null`. `null` matches only `null`. Otherwise 409 `test_command_changed`.
7. **The commit:** `sha` is reachable from the default branch. GitHub `GET /repos/{repo}/compare/{default_branch}...{sha}` must return `status` of `identical` or `behind`. Otherwise 409 `sha_not_on_default_branch`. A provider error gives 502 `provider_unavailable`, and nothing starts.
8. **The record:** `stage_runs.investigate = {status: "starting", detail: {repository_id, sha}}` is written. A second concurrent dispatch for the card then fails check 3. The write is a conditional update on the card row (`UPDATE … WHERE` the current `stage_runs` still has no live run).
9. **The run:** `POST {aifactory}/api/investigations {project_id: aifactory_project_id, sha, request, test_command, limits}` is sent with CFactory's AIFactory token, through `execute_action`'s client conventions (`B/actions.py:421-481`).
   - **201/200:** record `running` (or the state AIFactory returned) and `run_id`, and return `{run_id}`.
   - **A refusal (4xx/503):** record `refused` with AIFactory's `detail`, and return 409 with that detail.
   - **No response** (a timeout or connection error): record `uncertain` with no `run_id`, and return 504 `start_uncertain`. AIFactory's queued lease (60 s from insert) ends a run nobody renews. RFactory's plan already treats `uncertain` as blocking.
10. **Audit:** `investigate` is recorded with `ok`, the status code and the run id.

The checks happen in one call, not one transaction. The provider and AIFactory are remote, so a change between checks 6 and 9 is possible. The window is about the length of the compare call.

### 6. The relays (lease, stop, reads)

All of them are under `/api/cards/{key}/investigate/{run_id}`:

- **`POST …/lease`** (write) → AIFactory `POST /api/investigations/{run_id}/lease`. Not audited per call (every 10 s); the first and the last are.
- **`POST …/stop`** (write) → `POST …/stop`, audited.
- **`GET …`** (read) → `GET /api/investigations/{run_id}`, returning `{id, state, reason, pods_gone, tests, report, usage}`.
- **`GET …/logs?after=<seq>&limit=<n>`** (read) → `GET …/logs`, returning `{entries: [{seq, at, line}], cursor}`.
- **`GET /api/cards/{key}/investigate`** (read): recovery. It returns the card's recorded `stage_runs.investigate`, including `run_id` when known.

MCP: `cfactory_investigation_lease`, `cfactory_investigation_stop`, `cfactory_investigation_get`, `cfactory_investigation_logs` and `cfactory_investigation_recover`.

- **Ownership:** each relay first loads the card in the caller's tenant (deleted ones included), checks that it is an investigation card, and checks that `run_id` equals its recorded `run_id`. Otherwise 404. A run id alone is never enough, and only an id that passed this check reaches an upstream URL.
- **The board state:** on each `GET …`, the card's `stage_runs.investigate.status` and a `pods_gone` flag are updated from the response. That flag is what check 3 reads.
- **Responses are relayed field for field** (intent decision 4). RFactory's plan records the `pod_gone`/`pods_gone` rename and any other difference as a deviation.

### 7. Auth

- **Writes:** card create, dispatch, lease and stop use `require_scope("write")` (`B/auth.py:262`). They are additionally refused with 403 `keys_required` when the key store is open (dev-open, `B/auth.py:144`), so no deployment without keys can start a run.
- **MCP:** each tool gets a `TOOL_SCOPES` entry (`B/mcp.py:943-999`). The legacy `mcp_secret` keeps its full scope, as for every other tool. `tools/list` (`B/mcp.py:1544`) filters by the switch.
- **Tenant isolation is CFactory's existing model and is not strengthened here.** The tenant comes from the `X-Tenant-Id` header and is not tied to the key (`B/config.py:489-499`). The ownership check binds a run to a card within that tenant. A caller holding a write key can still name any tenant, exactly as it can for ordinary cards today. See Risks.

### 8. The AIFactory client

`B/investigations.py` (new) holds:
- the §4–§6 logic, used by both the REST routes (`B/routes_investigations.py`, new) and the MCP handlers;
- one small HTTP helper that reuses `settings.aifactory_api_url`, `settings.token_for("aifactory")`, the 10 s timeout and the transport seam (`action_transport_dep`, `B/api_deps.py:104-107`), so tests use `tests/cards_harness.py`'s `Upstream`.

`B/git_providers.py` gains, on `HttpGitHubProvider` only:
- `default_branch_head(repo) -> (branch, sha)`, built on the existing `GET /repos/{repo}` (`B/git_providers.py:405-417`);
- `is_reachable_from_default(repo, sha) -> bool`.

Other providers do not implement them, and the §4 check refuses them first.

### 9. Docs and deploy

- **`openapi.yaml`** (hand-maintained) gains the new routes and the `investigate_test_command` field. The Card schemas stay as they are.
- **The env table** (`techdocs/dependencies.md:93-102`) and `.env.example` get `CFACTORY_INVESTIGATIONS`.
- **Helm:** `charts/cfactory` gets an `investigations` value mapped in `templates/configmap.yaml`.
- **factory-gitops:** the CFactory manifest is left as it is. Turning the switch on is part of RFactory#18's rollout, not this change.

## Alternatives rejected

- **A no-tier card with no new field.** Any later tier, `ready` or sync turns it into real work or publishes it (intent, Problem).
- **A separate investigations table instead of a card.** The intent chose a card (decision 2) so the board shows it. A separate table would also need its own tenant scoping and soft delete.
- **`kind` on `CardCreate`/`CardUpdate`.** It would fail the external planning-card gate, and it would let a generic update flip a card's kind.
- **Taking the test command from the request.** The intent forbids that: speech never selects a command.
- **Head-only commit checks.** A commit landing between proposal and approval would refuse the approved run (intent decision 3).
- **Relaying a run id without a card check.** One tenant or key could then read, extend or stop another's run.
- **Supporting GitLab, Azure DevOps and Bitbucket now.** Their providers have `get_default_branch` but no reachability check. Fail closed until one is added and tested.

## Risks

- **Tenant isolation is header-based (existing).** A write key can act in any tenant it names. That is true of every CFactory write today. Strengthening it means binding keys to tenants, its own issue. Single-tenant deployments are unaffected.
- **The external conformance gate.** If the hub contract enumerates `stage_runs` keys, an `investigate` key could fail it. This is checked in the plan against the hub script; if it fails, the run is recorded on a separate column instead.
- **A run started but unrecorded.** CFactory crashes after AIFactory created the run but before writing `run_id`. The card shows `starting` with no id, and recovery returns no id. Nobody renews the lease, so AIFactory's 60 s lease ends the run, and its per-project slot blocks a second run until its pods are gone.
- **The check-to-create window** (§5). A repository change during the compare call is not caught. The window is seconds, and the command is re-sent and checked by value.
- **GitHub rate limits** on the compare call. Each dispatch makes one call, which is fine at meeting scale.
- **A stale board state:** `pods_gone` on the card updates only when someone reads the run. Check 3 relies on it, so a stale `false` refuses a new run (it fails closed) until a read refreshes it.

## Verification

- **Unit and route tests**, in `tests/test_investigations.py` (new), using `Upstream` and a fake GitHub transport:
  - each §5 refusal, one test per check, asserting that nothing reached AIFactory;
  - a `null` vs configured command and a configured vs `null` command are both refused, and `null` vs `null` dispatches with no tests;
  - `identical`, `behind`, `ahead` and `diverged` compare statuses, plus a provider error;
  - a timeout gives `uncertain` with no run id; a 4xx gives `refused`;
  - two concurrent dispatches: exactly one reaches AIFactory;
  - relays: a wrong `run_id`, a wrong tenant and a work card all give 404 with no upstream call; a deleted card's run can still be stopped and read;
  - responses are relayed field for field.
- **A guard test for every row of the §3 table:** each path, called on an investigation card, writes nothing and calls no provider. The `run` test checks that `stage_runs` is unchanged.
- **Auth:**
  - a read key gets 403 on all four writes, on REST and MCP;
  - dev-open with no keys gets 403 `keys_required`;
  - `tools/list` with `off`, `on` and `drain` shows exactly the expected investigation tools.
- **Migrations:** `tests/test_schema_bootstrap.py` passes, and a `create_all` store gains both columns.
- **Parity:** `tests/test_board_parity.py` still passes (the card models are unchanged), and the REST and MCP twins return equal bodies.
- **The full suite and the repository's CI gates**, including planning-card conformance, are green.
- **Live, as RFactory#18's step:** a real dispatch against AIFactory with the switch on, after AIFactory#2168 is released. That check is not part of this change.

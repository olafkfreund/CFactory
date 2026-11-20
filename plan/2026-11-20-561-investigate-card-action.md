---
status: approved
issue: 561
spec: spec/2026-11-20-561-investigate-card-action.md
coders:
  1: coder   # schema, migrations, store plumbing: routine but wide
  2: codex   # guards on ten paths plus their tests: small edits, easy to get subtly wrong
  3: codex   # executor path normalization: security-sensitive parsing
  4: coder   # switch, REST dependencies, repository command, context read, card creation, GitHub calls
  5: codex   # dispatch, reservation locks, outcome table, relays: the trickiest logic
  6: codex   # MCP tools, dev-open provenance, filtering, worker threads, parity: auth-sensitive
  7: agy     # docs, openapi.yaml, env table, Helm: large and mechanical
reviewers:
  - codex    # read-only diff review against this plan
  - agy      # independent second read-only review
---

# Plan: an `investigate` card action for read-only investigations

`B` means `apps/backend/cfactory/`. Work on branch `feat/561-investigate-card-action`, cut from `dev` after this plan is approved, in worktree `../CFactory-wt-561-impl`. Each step lands as one commit by the lead after its checks pass.

## Decisions (carried from the approved intent and spec)

### AIFactory's API, which this change only calls (PR olafkfreund/AIFactory#2168)

| Call | Response |
|---|---|
| `POST /api/investigations {project_id, sha, request, test_command, limits}` | 200 `{id, state}` |
| `GET /{id}` | `{id, state, reason, pods_gone, tests, report, usage: {tokens, turns, run_tests}}` |
| `GET /{id}/logs?after=0&limit=100` (`after ≥ 0`, `limit` 1 to 500) | `{entries: [{seq, at, line}], cursor}` |
| `POST /{id}/lease` | `{ok: true}` |
| `POST /{id}/stop` | `{state, pods_gone}` |

Final states are `completed`, `failed`, `stopped` and `expired`.

### The switch
- **`Settings.investigations: Literal["off","on","drain"] = "off"`** (`CFACTORY_INVESTIGATIONS`).
- **`off`:**
  - no routes;
  - no tools in `tools/list`;
  - a direct `tools/call` of an investigation tool returns "unknown tool", checked before scope.
- **`drain`:**
  - card create and dispatch return 503 `investigations_draining`, on REST and on direct MCP calls;
  - both tools are hidden from `tools/list`;
  - lease, stop, recovery and reads keep working.
- **Single-tenant only:** with `multi_tenant` on, every investigation route and tool returns 409 `multi_tenant_unsupported`. Investigation lookups also filter explicitly on the default tenant id. In single-tenant mode `cards_store_dep` returns an unscoped store (`B/api_deps.py:70-81`), and rows left from an earlier multi-tenant period must stay out of reach.
- **Rollback:**
  1. switch `on` → `drain`;
  2. wait until every card with a recorded `run_id` reads `pods_gone: true`, and AIFactory shows no live run for each investigated project;
  3. switch to `off`.

  Cards that were created but never dispatched do not block this.

### The card
- **Kind:** `CardRow.kind`, `"work"` or `"investigation"`, server default `"work"`.
  - It is never a field of `Card`, `CardCreate` or `CardUpdate`.
  - `Card` carries it as the `PrivateAttr` `_kind`. One store helper, `CardStore._card(row) -> Card`, sets it, and it replaces all 7 `Card.model_validate(row)` calls in `B/cards.py`: 817, 822, 837, 849, 869, 916 and 961, which include `get_by_issue_ref` and `cards_for_project`.
  - `Card.is_investigation` is a property reading `_kind`.
- **Contents:**
  - title: `Investigate: ` plus the first 200 characters of the request, with `…` when cut;
  - description: the full request, 1 to 2,000 characters;
  - tier, `issue_ref` and `correlation_key`: `None`;
  - status: `backlog`;
  - repository: an explicit `repository_id`.
- **`stage_runs.investigate`:** `{service: "aifactory", status, started_at, detail: {binding: {repository_id, repo, aifactory_project_id}, attempt, run_id, sha, pods_gone, reason}}`.
  - `binding` is fixed when the card is created.
  - The board shows only the state and the run id.
- **Guards:** an investigation card is refused before any write, project resolution or provider call by:
  - update, move and reprioritise;
  - `sync-github`, `sync_card` and `maybe_sync`;
  - `maybe_dispatch`, `dispatch_card`, `dispatch_stage` and `start_sequence`;
  - `apply_status`.

  REST maps the refusal to 409 `investigation_card`, never 500; MCP returns a tool error with the same reason.
- **Delete** stays a soft delete. Investigation lookups include deleted cards.

### The executor
- `execute_action` refuses any step, primary or follow-up, whose normalized **path** starts with `/api/investigations` (so `/api/investigationsX` is refused too). To normalize:
  1. take `urlsplit(endpoint).path`;
  2. loop `unquote` until the value is stable;
  3. take `urlsplit(...).path` again (this catches `%3F` and `%23`);
  4. collapse slashes;
  5. resolve dot segments;
  6. lowercase.
- A refusal returns 403 `investigations_via_executor` and is audited.
- **The `delete_task` prune:** on that REST route (`B/routes_actions.py:74`), a refused executor result must not delete the local work item.
- The rule applies in every switch state.

### The repository
- **The command field:** `GitRepositoryRow.investigate_test_command` (`Text`, nullable).
  - `""` is stored as `null`; at most 1,000 characters, with no `\n`, `\r` or NUL.
  - It is set through the existing repository update on REST and MCP, which needs the write scope. `repository_patch` (`B/git_connections.py:358`) and `repository_view` (`B/git_connections.py:480`) carry it, and the `cfactory_update_git_repository` input schema advertises it (`B/mcp.py:886`).
- **`GET /api/investigations/context?repository_id=`** (read scope) returns `{repository_id, repo, default_branch, sha, test_command}`.
  - With no `repository_id` it uses the tenant default and names the repository it used.
  - Refusals:
    - 404 `no_repository`;
    - 409 `no_aifactory_project`;
    - 409 `unsupported_provider` (only GitHub is supported);
    - 409 `empty_repository`;
    - 502 `provider_unavailable`.
- **`POST /api/investigations/cards {request, repository_id}`** (write scope) returns the `Card`.
  - `repository_id` is required, with no fallback, and the same repository refusals apply.
  - A request matching `ghp_`, `gho_`, `github_pat_`, `sk-`, `AKIA[0-9A-Z]{16}`, `-----BEGIN` or a `password=`/`token=` assignment is refused with 422 `request_contains_secret`.

### Dispatch: `POST /api/cards/{key}/actions/investigate {request, sha, test_command, limits}`, write scope

Steps in this order:
1. **The service:**
   - the switch is `on`, otherwise 503 in `drain` and no route when `off`;
   - the deployment is single-tenant;
   - the caller uses a credential the credential rule accepts.
2. **The inputs, without the card:**
   - `sha` is 40 lowercase hex characters;
   - `test_command` is a string or `null`, with `""` turned into `null`;
   - `limits` is optional and has only the keys `minutes`, `tokens`, `turns` and `test_calls`. Each must be a real `int`, not a `bool`, from 1 to 30, 2,000,000, 200 and 10 respectively. Omitted keys stay omitted.
3. **The card:**
   - it is in the tenant, not deleted, and an investigation card (otherwise 409 `not_investigation_card`);
   - then `request` must equal its description (otherwise 409 `request_changed`).
4. **No live run.**
   - Live means a recorded run id with pods not gone, or `starting` without a run id younger than 300 s.
   - A `starting` without a run id older than 300 s is read as `uncertain` (plan deviation 1).
   - A live run gives 409 `run_live`.
5. **The repository and the command:**
   - the binding's repository is re-read by its id, with no fallback (409 `repository_gone`);
   - it must still be on GitHub, with the same `repo` and the same `aifactory_project_id` (409 `repository_changed`);
   - the commands must be equal after both sides turn `""` into `null`, so `null` matches only `null` (409 `test_command_changed`).
6. **The commit:** GitHub's compare call, `GET /repos/{repo}/compare/{quote(branch, safe="")}...{sha}`.
   - `identical` or `behind` passes;
   - `ahead`, `diverged`, 404 or 422 give 409 `sha_not_on_default_branch`;
   - any other error gives 502 `provider_unavailable`.
7. **Reservation**, in one locked transaction (the store's no-op-`UPDATE` lock and re-read pattern):
   - re-check the card: same tenant, investigation, **not deleted**;
   - re-check steps 4 and 5;
   - only then write `{status: "starting", started_at: now, detail: {binding, attempt: <new uuid4>, sha, run_id: null, pods_gone: false}}`.

   If any re-check fails, nothing is written and that check's refusal is returned.
8. **Start the run:** `POST /api/investigations {project_id: binding.aifactory_project_id, sha, request, test_command, limits}`, with a 60 s timeout.
9. **Record the outcome**, in a locked write that applies only if `detail.attempt` still matches. Deleted cards are included.

   | AIFactory answer | Recorded | Returned |
   |---|---|---|
   | 200 with a non-empty string `id` | `running`, `run_id` | `{run_id}` |
   | 400, 403, 404, 409 or 422 | `refused`, with AIFactory's `detail` | 409 with that detail |
   | 503 | `refused`, reason `investigations_draining` | 503 |
   | Anything else, a malformed 200, a timeout or a connection error | `uncertain`, `run_id: null` | 504 `start_uncertain` |
10. **Audit**, with the layout in plan deviation 2.

### Relays

All are under `/api/cards/{key}/investigate/{run_id}`, plus a recovery route:

| Route | Scope | Upstream timeout | Audited |
|---|---|---|---|
| `POST …/lease` | write | 10 s | every call |
| `POST …/stop` | write | 45 s | every call |
| `GET …` | read | 10 s | no |
| `GET …/logs?after=0&limit=100` | read | 10 s | no |
| `GET /api/cards/{key}/investigate` | read | none | no |

- **The recovery route** returns the local `stage_runs.investigate` record. It takes no run id and makes no upstream call.
- **Ownership:** every run-id route first loads the card in the tenant (deleted included) and checks that it is an investigation card and that its recorded `run_id` matches. Otherwise 404.
- **Recording the state:** after `GET …` and stop, the state and `pods_gone` are recorded in a locked write. It applies only while the `run_id` still matches, and it is monotonic: `pods_gone` never goes from true to false, and a final state never becomes live again.
- **Stop:** a stop timeout or 5xx records `stopping` and returns 504 `stop_uncertain`.
- **Responses:** relayed field for field.

### Credentials and MCP
- **REST writes** (card create, dispatch, lease, stop) need the write scope **and** a non-open key store, injected through `keystore_dep` (`B/auth.py:262-279`) so tests can override it. Dev-open gives 403 `keys_required`.
- **MCP writes** accept a matched write-scoped key or the legacy `mcp_secret`. They refuse access that came only from `mcp_dev_open`.
- **Reads** need the read scope.
- **The eight MCP tools:**
  - `cfactory_investigation_context`
  - `cfactory_create_investigation_card`
  - `cfactory_investigate_card`
  - `cfactory_investigation_lease`
  - `cfactory_investigation_stop`
  - `cfactory_investigation_get`
  - `cfactory_investigation_logs`
  - `cfactory_investigation_recover`

  Their handlers run the shared synchronous code in a worker thread (`run_in_threadpool` or `anyio.to_thread`), so a 60 s dispatch never blocks `/mcp`.

### Rejected (see the spec)
- a no-tier card;
- a public `kind` field;
- a separate table;
- trusting the tenant header;
- leaving the executor open;
- a JSON `UPDATE … WHERE`;
- the shared 10 s timeout;
- the command from the request;
- head-only commit checks;
- an AIFactory lookup API now;
- providers other than GitHub;
- a `@model_validator` on `Card`, which the mypy ratchet blocks (see the `CardUpdate.model_post_init` docstring, `B/cards.py:483-492`).

### Deviations from the spec (made while planning)
1. **A stale `starting` counts as `uncertain`.** The spec leaves no way out of `starting` if CFactory exits between the reservation and recording the outcome. The card would refuse every later dispatch with `run_live`. A `starting` without a run id older than 300 s is therefore read as `uncertain`: the create call's 60 s read timeout is per phase, so the whole call may exceed 120 s. AIFactory's slot and lease cover a run that might exist, exactly as for any `uncertain`.
2. **Where the audit fields go.** `AuditStore.record` (`B/audit.py:412-441`) has fixed fields and no metadata. The spec's hashes and ids are therefore packed into `endpoint` (`String(512)`), with `correlation_key` set to the card key or `repo:<id>`:

   | kind | endpoint |
   |---|---|
   | `set_investigate_test_command` | `repo/<id>:old=<sha256>;new=<sha256>`, both read from the row inside the update's own transaction |
   | `create_investigation_card` | `card/<key>:request=<sha256>` |
   | `investigate` | `card/<key>:attempt=<uuid>;run=<id or ->;outcome=<status>` |
   | `investigation_lease` and `investigation_stop` | `card/<key>:run=<id>` |
   | `investigation_executor_refused` | the refused endpoint, truncated to 512 |

   Neither the request text nor the command text is ever stored. The spec's §2 mitigation (one store helper sets `_kind`) is kept as it is.

The hub conformance gate (`Factory/scripts/check_planning_card_conformance.py`) compares the card models' top-level field facets and `additionalProperties`. It does not inspect keys inside `Card.stage_runs` (`dict[str, Any]`, `B/cards.py:362`), so `stage_runs.investigate` closes the spec's conformance risk without a new column. Both reviewers confirmed this against the script.

## Deviations found during implementation

1. **Step 1: `update_repository(..., previous=None)`** takes an optional dict that it fills with the old values from inside its own transaction. The plan had it return the old value. The change keeps the existing `GitRepositoryRow` return type, which `git_config_ops.py:675` and the tests rely on, so steps 4 and 5 pass `previous={}` to get the old command for the audit hashes.
2. **Step 1: `tests/test_git_mixed_tenant.py`** used to upgrade to `head` and then downgrade one step. That only worked while its own revision was the head, so it now upgrades to `7e4c2a91d6b8`.
3. **Step 3: `_investigation_path`** prefixes `/` and collapses repeated slashes *before* `posixpath.normpath`, and the order matters:
   - **Why prefix:** relative endpoints such as `api/investigations` pass `is_safe_endpoint` and are joined onto the base URL, so they must be anchored before dot segments are resolved.
   - **Why collapse after prefixing:** `normpath` keeps a leading `//`.

   Both forms are now covered by tests.
4. **Step 2 (Codex): `issue_import` also skips investigation cards.** Both `_import_one` and the comment sync (`_sync_comments`) skip them. The plan's own check requires that an import never touches one, and this is where that is enforced.
5. **Step 4: refusals and the repository lookup.**
   - **Signatures:** `context()` and `create_card()` take an `AuditContext` rather than separate audit and actor arguments.
   - **Refusals** are HTTP errors with `detail={"reason": ...}`.
   - **`_repository`** also refuses with 404 `no_repository` when the repository's connection is gone or its tenant is not `default`.
   - **Empty repositories:** card creation reads the default-branch head, so an empty repository or a failing provider refuses creation as well. That follows from "the same repository refusals apply".
6. **Step 5 (Codex): three small departures.**
   - **An empty repository** at dispatch refuses with `sha_not_on_default_branch`: no commit can be on a branch that has none.
   - **An AIFactory refusal** returns 409 with reason `upstream_refused` and AIFactory's `detail`.
   - **`_aifactory`** takes `Settings` explicitly.
   - **Not tested on PostgreSQL:** the concurrency test ran on SQLite only, because CI has no PostgreSQL job. Recorded for the PR.
7. **Step 5: use phase-specific HTTP timeouts and a 300 s stale window.** A 60 s httpx timeout applies to each network phase, not the entire create request. The former 120 s stale window could expire while a create was still in progress. Create now uses connect/write/pool 10 s and read 60 s; stop uses read 45 s. A `starting` record without a run id becomes stale after 300 s.
8. **Review round 2 (lead): a run AIFactory has forgotten frees its card.** Once a recorded run stays live until `pods_gone` (round 1), a run AIFactory no longer knows would block its card forever. An AIFactory 404 whose detail is exactly `run not found`, on a read or a stop, records `state: gone, pods_gone: true`. Any other 404, such as a lost project membership, leaves the card live. The reservation also checks the connection's tenant.

## Steps

Checks use the main checkout's virtualenv:

```bash
cd ../CFactory-wt-561-impl
export PYTHONPATH=apps/backend PY=../CFactory/apps/backend/.venv/bin
$PY/pytest -q tests/<file>
$PY/ruff check --config ruff.toml apps/backend/cfactory scripts
$PY/ruff format --check --config ruff.toml apps/backend/cfactory scripts
```

Traps that apply to every step:
- **Tests** live in `tests/`. `tests/conftest.py:25` sets `CFACTORY_API_DEV_OPEN=true`, so dev-open tests set their own environment. `get_settings` is cached (`B/config.py:502`): call `reset_settings()` (`B/config.py:511`) before rebuilding the app with a different switch.
- **The app** is built in `B/app.py:create_app` (routers at `B/app.py:191-216`).
- **The Alembic head** is `7e4c2a91d6b8`. Exactly one head must remain.
- **`_LATE_COLUMNS` values** are the clause *after* the type, never the type (`B/cards.py:615-621`, rendered by `B/db.py:75`).
- **Card models:** no field is added to `Card`, `CardCreate` or `CardUpdate`, and there is no `@model_validator` in `B/cards.py` (the mypy ratchet).
- **The CI ratchet** checks ruff and mypy per changed file (`scripts/ratchet_lint.py`). New code must be ruff-clean and fully typed.
- **Implementers:** do not stage, commit, push, deploy or restart. Report the changed files and any deviation.

1. **Schema and store** (`B/cards.py`, `B/git_connections.py`, `apps/backend/migrations/versions/`):
   - **Columns:**
     - `CardRow.kind: Mapped[str] = mapped_column(String(16), nullable=False, default="work", server_default="work")`;
     - `GitRepositoryRow.investigate_test_command: Mapped[str | None] = mapped_column(Text, nullable=True)`.
   - **Migrations:** two chained revisions, `<rev1>_card_kind.py` (down `7e4c2a91d6b8`, `server_default="work"`) and `<rev2>_repository_investigate_test_command.py` (down `<rev1>`). Both use `batch_alter_table` as in `7e4c2a91d6b8_mixed_tenant_columns.py:21-32`.
   - **Late columns:** `_LATE_COLUMNS["kind"] = "NOT NULL DEFAULT 'work'"` and `_LATE_REPOSITORY_COLUMNS["investigate_test_command"] = ""`.
   - **`Card`:** a `PrivateAttr` `_kind: str = "work"` and the property `is_investigation`. `CardStore._card(row)` validates the row, sets `_kind = row.kind` and returns the card, and it replaces all 7 `Card.model_validate(row)` calls.
   - **Repository plumbing:**
     - `GitRepository` exposes `investigate_test_command`;
     - `GitRepositoryUpdate` accepts it, validated;
     - `repository_patch` and `repository_view` carry it;
     - `update_repository` returns the old value from inside its transaction, for the audit hashes.
   - **Store reads:** `get` and `_get_row` gain `include_deleted: bool = False`.
   - **New store methods:**
     - `create_investigation_card(*, title, description, repository_id, binding)`;
     - `locked_investigation(card_key, tenant_id)`, a context manager yielding `(session, row)` after the no-op-`UPDATE` lock and a re-read, deleted cards included, modelled on `_lock_connections` (`B/cards.py:1233-1245`);
     - `write_investigation(session, row, record)`, which assigns a new dict to `stage_runs`.
   - **Check:** `$PY/pytest -q tests/test_schema_bootstrap.py tests/test_cards_api.py tests/test_git_config.py tests/test_git_multi_connection.py`. New `tests/test_investigation_store.py` covers:
     - `_kind` through all 7 loaders, with `issue_ref` and `correlation_key` seeded so `get_by_issue_ref`, `get_by_correlation_key` and `cards_for_project` are exercised;
     - `Card.model_json_schema()` and `model_dump()` unchanged;
     - an existing row reading `kind == "work"` after the migration and after the late-column upgrade;
     - `include_deleted`;
     - the command normalization and its REST round trip;
     - `stage_runs` changes being persisted.

     `alembic heads` must show one head.

2. **Guards** (`B/card_intake.py`, `B/github_sync.py`, `B/card_ops.py`, `B/routes_cards.py`, `B/mcp.py`):
   - **The error:** `InvestigationCardError(ValueError)` in `B/card_intake.py`, with `reason = "investigation_card"`.
   - **The guards, each placed as its path's first statement after the card is loaded:**
     - `card_ops.update_card`: load with `store.get`, then guard, before `store.update` (`B/card_ops.py:362`);
     - `sync_card_github`: after `get_card` (`B/card_ops.py:389`);
     - `github_sync.sync_card`: first line (302), returning `{ok: False, skipped: "investigation_card"}`;
     - `maybe_sync`: first line (384), returning `None`;
     - `maybe_dispatch`: first line (663), returning `None`;
     - `dispatch_card`: first line (620), returning the refusal with no write;
     - `dispatch_stage`: first line (682), raising;
     - `start_sequence`: first line (728), raising;
     - `apply_status`: after `get_by_correlation_key` (856), ignoring the card.
   - **Error mapping:** `routes_cards.update_card`, `routes_cards.sync_card_github` and `_stage_action` (`B/routes_cards.py:74`) map the error to 409 `{"detail": {"reason": "investigation_card"}}`. The MCP handlers for update, move, reprioritise, sync and the stage actions return a tool error with that reason.
   - **Check:** new `tests/test_investigation_guards.py`, with one test per guard:
     - each direct helper's result is asserted separately from the REST 409 and the MCP tool error;
     - the fixtures for `maybe_sync`, `maybe_dispatch` and `apply_status` are set up so that, without the guard, they would reach the sync or dispatch. For example the card has `status: ready` and a tier patched into the row, or a sync-enabled target and project, or a matching `correlation_key` set on the row;
     - each test asserts unchanged `stage_runs`, status and tier, and no `Upstream` or provider call;
     - `issue_import` never touches an investigation card.

     Then `$PY/pytest -q tests/test_card_intake.py tests/test_card_stage_actions.py tests/test_github_sync.py tests/test_cards_api.py tests/test_mcp_board_tools.py tests/test_board_parity.py tests/test_issue_import.py` stays green.

3. **Close the executor** (`B/actions.py`, `B/routes_actions.py`):
   - `_investigation_path(endpoint) -> bool`, implementing the six normalization steps in Decisions. It is checked in `execute_action` (`B/actions.py:421`) over the primary step and every follow-up, before any request.
   - A match returns `{ok: False, status_code: 403, reason: "investigations_via_executor", …}` in the existing result shape.
   - `routes_actions.execute_prepared_action` audits it as `investigation_executor_refused`, and skips the `delete_task` local prune (`B/routes_actions.py:74`) when the result was refused.
   - **Check:** extend `tests/test_actions.py` (existing, around line 475). These endpoints must be refused as the primary step and as a follow-up, with no `Upstream` call:
     - `/api/investigations`, `/API/Investigations/x`, `/api/investigationsX`
     - `/api/tasks/../investigations`, `/api//investigations`
     - `%2Fapi%2Finvestigations`, `/api/investigations%2F1`
     - `/api/investigations?x=1`, `/api/investigations%3Fx=1`, `/api/investigations#x`

     Through REST, also assert the audit row, and that a refused `delete_task` leaves the work item in place. Existing actions still run.

4. **The switch, REST dependencies, the repository command, the proposal read and card creation** (`B/config.py`, `B/auth.py`, `B/git_providers.py`, `B/investigations.py` (new), `B/routes_investigations.py` (new), `B/app.py`, `B/routes_git_config.py` / `B/git_config_ops.py`, `B/mcp.py:886`):
   - **The setting:** `investigations: Literal["off","on","drain"] = "off"`, next to `jira_enabled` (`B/config.py:217`).
   - **REST dependencies,** in `B/routes_investigations.py`:
     - `investigation_write(op)` requires `require_scope("write")`; raises 403 `keys_required` when the injected `keystore_dep` store is open; raises 409 `multi_tenant_unsupported` when `settings.multi_tenant`; and raises 503 `investigations_draining` for `create` and `dispatch` in `drain`;
     - `investigation_read` requires the read scope plus the tenancy check.
   - **The router** is mounted in `create_app` (`B/app.py:191-216`) only when the switch is not `off`.
   - **GitHub provider methods,** on `HttpGitHubProvider` only, using its `_client` and `_TIMEOUT_SECONDS` and bridged with `run_sync` (`B/git_providers.py:153-167`):
     - `default_branch_head(repo) -> tuple[str, str]`: `GET /repos/{repo}`, then `GET /repos/{repo}/commits/{quote(branch, safe="")}`. An empty repository raises `EmptyRepository`.
     - `is_reachable_from_default(repo, branch, sha) -> bool`, using the compare mapping from Decisions.

     The provider is built for the bound repository through the existing `build_provider` (`B/git_providers.py:486`) and its connection credential, with the transport seam passed through.
   - **`B/investigations.py`:**
     - `context(store, settings, repository_id, transport)`;
     - `create_card(store, audit, actor, settings, request, repository_id, transport)`, which applies the secret patterns, the title cap and the audit;
     - `_repository(store, repository_id, *, fallback: bool)`, which falls back to the default only for `context`;
     - investigation lookups filter on the default tenant id.
   - **The command:** the repository update's REST and MCP paths audit `set_investigate_test_command` using the old value from step 1. The MCP `cfactory_update_git_repository` input schema (`B/mcp.py:886`) lists the field.
   - **Check:** new `tests/test_investigations_context.py`, with a fake GitHub transport, covers:
     - each refusal code;
     - the context falling back to the default while card creation requires an id;
     - a slash in a branch name;
     - an empty repository;
     - the secret-pattern refusals;
     - the title capped at 214 characters with the full description kept;
     - audit rows that carry hashes, not text;
     - the REST and MCP command round trips;
     - the router absent when off;
     - the write dependency giving `keys_required` under REST dev-open;
     - the tenancy refusal.

5. **Dispatch and relays** (`B/investigations.py`, `B/routes_investigations.py`):
   - **`dispatch(...)`** implements steps 1–10 exactly as in Decisions, using the step 1 lock helpers. `_aifactory(method, path, json, timeout, transport) -> tuple[int, dict | None]` is built on `settings.aifactory_api_url`, `settings.token_for("aifactory")` and the `action_transport_dep` transport. It never raises on HTTP errors; a timeout or connection error returns status 0.
   - **Relays:** `lease`, `stop`, `get_run`, `logs` and `recover`.
   - **Routes:**
     - `POST /api/cards/{key}/actions/investigate`
     - `POST /api/cards/{key}/investigate/{run_id}/lease` and `…/stop`
     - `GET /api/cards/{key}/investigate/{run_id}` and `…/logs`
     - `GET /api/cards/{key}/investigate`
   - **Check:** new `tests/test_investigations_dispatch.py`, covering:
     - one test per refusal, with no AIFactory call;
     - the three command cases, plus `""` versus `null`;
     - limits that are `True`, `0`, above the cap, or use an unknown key;
     - the seven compare results;
     - a repository changed during compare: refused at reservation;
     - **a card deleted during compare: refused at reservation, nothing sent**;
     - a card deleted during the AIFactory call: the outcome is still recorded;
     - two concurrent dispatches through a `threading.Barrier`-gated fake compare: exactly one reaches AIFactory;
     - a slow read of run A after run B is reserved: B untouched;
     - every row of the outcome table, including a malformed 200 and a 500;
     - a stale `starting` (older than 300 s) is not live, and a fresh one is;
     - relay ownership with a wrong `run_id`, a work card, and a deleted card that is both readable **and** stoppable;
     - monotonic `pods_gone` and final states;
     - a stop timeout recording `stopping`;
     - recovery making no upstream call;
     - field-for-field relaying;
     - each audit layout row.

   Trap: never write `stage_runs` outside the lock. Run the race test on PostgreSQL as well if CI's PostgreSQL job runs these tests (read `.github/workflows/test.yml` for how it gets its URL). Otherwise run it locally against a disposable PostgreSQL and record the result in the PR.

6. **MCP** (`B/mcp.py`, `tests/test_board_parity.py`):
   - **Dev-open provenance:** `_authenticate` (`B/mcp.py:1009-1057`) returns, or puts on the tool context, whether access came from `mcp_dev_open`, a scoped key or the legacy secret.
   - **Tools:** the eight tools get `MCP_TOOLS`, `TOOL_SCOPES` (writes `WRITE`, reads `READ`) and `_TOOL_HANDLERS` entries.
   - **Handlers** call `B/investigations.py` in a worker thread, and apply the credential rule, the tenancy rule and drain.
   - **`tools/call`** (`B/mcp.py:1544-1550`) resolves the switch **before** the scope check: an investigation tool when off is "unknown tool". `tools/list` filters by the switch.
   - **Parity:** `tests/test_board_parity.py` (`BOARD_ROUTE_PREFIXES`, `BOARD_TOOL_MARKERS` at 41-42) is updated. The default-app run excludes investigation tools and routes, and a new parity test with `investigations="on"` pairs every investigation route with its tool.
   - **Check:** new `tests/test_investigations_mcp.py`, covering:
     - a read key gets a scope error on all four writes;
     - MCP dev-open with no keys gives `keys_required`;
     - the legacy secret is accepted;
     - `multi_tenant` on refuses every tool;
     - `tools/list` and direct `tools/call` in `off`, `on` and `drain`, including a read-scoped direct call when off getting "unknown tool";
     - while a dispatch is blocked in the fake AIFactory, a second `/mcp` request answers.

     Then the full suite, `$PY/pytest -q tests`.

7. **Docs and deploy** (`openapi.yaml`, `techdocs/dependencies.md:93-102`, `.env.example`, `charts/cfactory/values.yaml`, `charts/cfactory/templates/configmap.yaml`, `docs/guides/token-gated-api.md`):
   - add the new routes and `investigate_test_command` to `openapi.yaml`, with the Card schemas unchanged;
   - document `CFACTORY_INVESTIGATIONS` and the rollback;
   - add the Helm value `investigations: off`, mapped to the env.

   **Check:** `just helm-template | grep CFACTORY_INVESTIGATIONS`, `just helm-lint`, and `python -c 'import yaml; yaml.safe_load(open("openapi.yaml"))'`. Also run `openapi-spec-validator openapi.yaml` if it is installable.

8. **Gates (lead):**
   - the full suite;
   - ruff check and format;
   - `python scripts/ratchet_lint.py --base origin/dev --package apps/backend/cfactory --package scripts`, and the same with `--tool mypy`;
   - one Alembic head;
   - the planning-card conformance script run locally against `../Factory` with the arguments the workflow uses (`.github/workflows/planning-card-conformance.yml`), which must pass.

9. **Reviews (lead):** a fresh Opus agent given only this plan and `git diff origin/dev`, plus Codex and agy, all read-only. Confirmed findings are fixed with tests that fail on the old code, and step 8 is rerun.

10. **PR to `dev` (lead):** link the intent, spec and plan, say which steps Codex, agy and the coder did, and state that the switch is off by default. Tell the release lead on the agent bus.

## Tests

Run in the implementation worktree, with 0 failures:

```bash
PYTHONPATH=apps/backend ../CFactory/apps/backend/.venv/bin/pytest -q tests
../CFactory/apps/backend/.venv/bin/ruff check --config ruff.toml apps/backend/cfactory scripts
../CFactory/apps/backend/.venv/bin/ruff format --check --config ruff.toml apps/backend/cfactory scripts
python scripts/ratchet_lint.py --base origin/dev --package apps/backend/cfactory --package scripts
python scripts/ratchet_lint.py --tool mypy --base origin/dev --package apps/backend/cfactory --package scripts
just helm-lint
```

## Rollback

- **Turn it off:**
  1. Set `CFACTORY_INVESTIGATIONS=drain` and restart.
  2. Wait until every card with a recorded `run_id` reads `pods_gone: true`, and AIFactory shows no live run for each investigated project.
  3. Set it to `off`.

  Investigation cards stay inert, because the guards do not depend on the switch.
- **Revert the code:** revert the PR.
  - Both migrations only add nullable or defaulted columns, and `downgrade` drops them.
  - **Before downgrading,** hard-delete every investigation card, deleted ones included: `DELETE FROM cards WHERE kind = 'investigation'`, after exporting them, if the run records are wanted, with `SELECT card_key, stage_runs FROM cards WHERE kind = 'investigation'`. Otherwise they would come back as ordinary no-tier cards.
  - The executor refusal goes with the revert, which reopens the gap the spec describes.

---
status: draft
issue: 561
spec: spec/2026-11-20-561-investigate-card-action.md
coders:
  1: coder   # schema, migrations, store plumbing: routine but wide
  2: codex   # guards on ten paths plus their tests: small edits, easy to get subtly wrong
  3: codex   # executor path normalization: security-sensitive parsing
  4: coder   # repository command field, context read, card creation, GitHub provider calls
  5: codex   # dispatch, reservation locks, outcome table, relays: the trickiest logic
  6: coder   # switch, credential rule, tenancy refusal, MCP registration and filtering
  7: agy     # docs, openapi.yaml, env table, Helm: large and mechanical
reviewers:
  - codex    # read-only diff review against this plan
  - agy      # independent second read-only review
---

# Plan: an `investigate` card action for read-only investigations

`B` means `apps/backend/cfactory/`. Work on branch `feat/561-investigate-card-action`, cut from `dev` after this plan is approved, in worktree `../CFactory-wt-561-impl`. Each step lands as one commit by the lead after its checks pass.

## Decisions (carried from the approved intent and spec)

**The switch**
- `Settings.investigations: Literal["off","on","drain"] = "off"` (`CFACTORY_INVESTIGATIONS`).
  - `off`: no routes and no tools; a direct `tools/call` returns "unknown tool".
  - `drain`: card create and dispatch return 503 `investigations_draining`, on REST and on direct MCP calls; both tools are hidden from `tools/list`; lease, stop, recover and reads keep working.
- **Single-tenant only:** with `multi_tenant` on, every investigation route and tool returns 409 `multi_tenant_unsupported`.
- **Rollback:** `on` → `drain` → `off`, only after every recorded run reads `pods_gone: true` and AIFactory shows no live run for each investigated project.

**The card**
- **Kind:** `CardRow.kind` is `"work" | "investigation"`, server default `"work"`. It is never a field of `Card`, `CardCreate` or `CardUpdate`. `Card` carries it as a `PrivateAttr` `_kind`.
- **Contents:**
  - title: `Investigate: ` plus the first 200 characters of the request, with `…` when cut;
  - description: the full request, 1 to 2,000 characters;
  - tier, `issue_ref` and `correlation_key`: `None`;
  - status: `backlog`;
  - repository: an explicit `repository_id`.
- **`stage_runs.investigate`:** `{service: "aifactory", status, detail: {binding: {repository_id, repo, aifactory_project_id}, attempt, run_id, sha, pods_gone}}`. `binding` is fixed when the card is created.
- **Guards:** an investigation card is refused before any write or provider call by:
  - update, move and reprioritise;
  - `sync-github`, `sync_card` and `maybe_sync`;
  - `maybe_dispatch`, `dispatch_card`, `dispatch_stage` and `start_sequence`;
  - `apply_status`.

  REST maps the refusal to 409 `investigation_card`, never 500.
- **Delete** stays a soft delete, and investigation lookups include deleted cards.

**The executor**
- `execute_action` refuses every step, primary or follow-up, whose endpoint normalizes to a path starting with `/api/investigations`. Normalizing means percent-decode, resolve dot segments, collapse slashes, lowercase.
- The refusal is 403 `investigations_via_executor` and is audited.
- This applies in every switch state.

**The repository**
- **Command:** `GitRepositoryRow.investigate_test_command` (`Text`, nullable).
  - `""` is stored as `null`; at most 1,000 characters, with no newline or NUL.
  - Set through the existing repository update, which needs the write scope.
  - Audited as `set_investigate_test_command`, with SHA-256 hashes of the old and new values.
- **Proposal read:** `GET /api/investigations/context?repository_id=` (read scope) returns `{repository_id, repo, default_branch, sha, test_command}`. Refusals:
  - 404 `no_repository`;
  - 409 `no_aifactory_project`;
  - 409 `unsupported_provider` (only GitHub is supported);
  - 409 `empty_repository`;
  - 502 `provider_unavailable`.
- **Card creation:** `POST /api/investigations/cards {request, repository_id}` (write scope).
  - Same repository refusals as the proposal read.
  - Requests matching `ghp_`, `gho_`, `github_pat_`, `sk-`, `AKIA[0-9A-Z]{16}`, `-----BEGIN` or a `password=`/`token=` assignment are refused with 422 `request_contains_secret`.
  - Audited with the request's SHA-256.

**Dispatch:** `POST /api/cards/{key}/actions/investigate {request, sha, test_command, limits}` (write scope). The checks run in this order:
1. **The service:** switch, tenancy, credential.
2. **The inputs:**
   - `sha` is 40 lowercase hex characters;
   - `request` equals the card's description;
   - `limits` uses only the keys `minutes`, `tokens`, `turns` and `test_calls`, each at most 30, 2,000,000, 200 and 10.
3. **The card:** in the tenant, not deleted, an investigation card. Otherwise 409 `not_investigation_card`.
4. **No live run:**
   - Live means `starting`, `running` or `stopping`, or a final state whose pods are not yet gone.
   - `refused` and `uncertain` do not count as live.
   - A live run gives 409 `run_live`.
5. **The repository and the command:**
   - the repository is re-read by its id, with no fallback (409 `repository_gone`);
   - it must still be on GitHub, with the same `repo` and the same `aifactory_project_id` (409 `repository_changed`);
   - the approved command must equal the configured one, where `null` matches only `null` (409 `test_command_changed`).
6. **The commit**, through GitHub's compare call:
   - `identical` or `behind` passes;
   - `ahead`, `diverged`, 404 or 422 give 409 `sha_not_on_default_branch`;
   - any other error gives 502.
7. **Reservation:** inside one locked transaction (the store's no-op-`UPDATE` lock and re-read pattern), checks 4 and 5 run again. Only then is `starting` written, with a new attempt uuid.
8. **The AIFactory call:** `POST /api/investigations`, with a 60 s timeout.
9. **The outcome is recorded** in a locked write that applies only if the attempt still matches, deleted cards included:

   | AIFactory answer | Recorded | Returned |
   |---|---|---|
   | 200 with a string `id` | `running`, `run_id` | `{run_id}` |
   | 400, 403, 404, 409 or 422 | `refused` | 409 with AIFactory's detail |
   | 503 | `refused` | 503 |
   | Anything else, a malformed 200, a timeout or a connection error | `uncertain`, `run_id: null` | 504 `start_uncertain` |
10. **Audit.**

**Relays.** All live under `/api/cards/{key}/investigate/{run_id}`. Each one first loads the card in the tenant (deleted included), checks it is an investigation card and that its recorded `run_id` matches; otherwise 404.

| Route | Scope | Timeout | Audited |
|---|---|---|---|
| `POST …/lease` | write | 10 s | every call |
| `POST …/stop` | write | 45 s | every call; a timeout or 5xx records `stopping` and returns 504 `stop_uncertain` |
| `GET …` | read | 10 s | no |
| `GET …/logs?after>=0&limit 1..500` | read | 10 s | no |
| `GET /api/cards/{key}/investigate` (recovery) | read | – | no |

- After `GET …` and stop, the run's state and `pods_gone` are recorded in a locked write.
  - It applies only if the `run_id` still matches.
  - It is monotonic: `pods_gone` never goes from true to false, and a final state never becomes live again.
- AIFactory's responses are relayed field for field.

**Credentials**
- **REST:** investigation writes need the write scope **and** a configured key, so dev-open gives 403 `keys_required`.
- **MCP:** investigation writes accept a matched write-scoped key or the legacy `mcp_secret`. They refuse access that came only from `mcp_dev_open`.
- **Reads** need the read scope.

**MCP tools:** `cfactory_investigation_context`, `cfactory_create_investigation_card`, `cfactory_investigate_card`, `cfactory_investigation_lease`, `cfactory_investigation_stop`, `cfactory_investigation_get`, `cfactory_investigation_logs` and `cfactory_investigation_recover`.

**Rejected (see the spec):**
- a no-tier card;
- a public `kind` field;
- a separate table;
- trusting the tenant header;
- leaving the executor open;
- a JSON `UPDATE … WHERE`;
- the shared 10 s timeout;
- taking the command from the request;
- head-only commit checks;
- an AIFactory lookup API now;
- providers other than GitHub.

### Deviation from the spec (made while planning)

1. **Setting `_kind`:** the spec's mitigation is "one constructor helper in the store". All nine `Card` constructions in `B/cards.py` (817, 822, 837, 849, 869, 916, 961, …) already go through `Card.model_validate(row)`. A `@model_validator(mode="wrap")` on `Card` therefore sets `_kind` from the source row's `kind` (or a dict's `"kind"`, default `"work"`) on every construction, with nothing for a caller to forget. A wrap validator does not change `model_json_schema()`, so the conformance gate is unaffected.
2. **The spec's conformance risk is closed:** `Card.stage_runs` is `dict[str, Any]` (`B/cards.py:362`), and the hub gate (`Factory/scripts/check_planning_card_conformance.py`) compares field names, types, enums and `additionalProperties`, not the keys inside a free dict. `stage_runs.investigate` needs no separate column.

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
- **Tests** live in `tests/`, not `apps/backend/tests`. `tests/conftest.py` sets `CFACTORY_API_DEV_OPEN=true`, so the dev-open tests must override it.
- **The Alembic head** is `7e4c2a91d6b8`. Exactly one new head is allowed per revision chain: step 1 adds two revisions, chained.
- **`create_all`** never alters existing tables. Columns also go into `_LATE_COLUMNS` (`B/cards.py:622-637`) and `_LATE_REPOSITORY_COLUMNS` (`B/cards.py:644`).
- **Generic card models:** never add a field to `Card`, `CardCreate` or `CardUpdate`.
- **The CI gates** ratchet ruff and mypy per changed file (`scripts/ratchet_lint.py`). New code must be ruff-clean and fully typed.
- **Implementers:** do not stage, commit, push, deploy or restart. Report the changed files and any deviation.

1. **Schema and store plumbing** (`B/cards.py`, `B/git_connections.py`, `apps/backend/migrations/versions/`):
   - **Columns:** add `CardRow.kind: Mapped[str] = mapped_column(String(16), nullable=False, default="work", server_default="work")` and `GitRepositoryRow.investigate_test_command: Mapped[str | None] = mapped_column(Text, nullable=True)`.
   - **Migrations:** two revisions, `<rev1>_card_kind.py` (down_revision `7e4c2a91d6b8`) then `<rev2>_repository_investigate_test_command.py` (down_revision `<rev1>`). Use `batch_alter_table` as in `7e4c2a91d6b8_mixed_tenant_columns.py:21-32`.
   - **Late columns:** `_LATE_COLUMNS["kind"] = "VARCHAR(16) NOT NULL DEFAULT 'work'"` and the repository entry in `_LATE_REPOSITORY_COLUMNS`.
   - **`Card._kind`:** a `PrivateAttr(default="work")` plus the wrap validator (plan deviation 1), and the property `Card.is_investigation`.
   - **Repository model:** `GitRepository` exposes `investigate_test_command`. `GitRepositoryUpdate` accepts it, validated: at most 1,000 characters, no `\n`, `\r` or NUL, and `""` stored as `None`.
   - **Store reads:** `CardStore.get(card_key, *, include_deleted=False)` and `_get_row(..., include_deleted=False)` (`B/cards.py:794-822`).
   - **New store methods:**
     - `create_investigation_card(*, title, description, repository_id, binding)`, which sets `kind="investigation"` and `stage_runs={"investigate": {service, status: "created", detail: {binding, attempt: None, run_id: None, sha: None, pods_gone: False}}}`;
     - `locked_investigation(card_key)`, a context manager yielding `(session, row)` after the no-op-`UPDATE` lock and a re-read, deleted cards included, modelled on `_lock_connections` (`B/cards.py:1233-1245`);
     - `write_investigation(session, row, record)`, which replaces `stage_runs["investigate"]` (assigning a new dict so SQLAlchemy sees the change).
   - **Check:** `$PY/pytest -q tests/test_schema_bootstrap.py tests/test_cards_api.py tests/test_git_config.py` passes. New `tests/test_investigation_store.py` asserts:
     - `_kind` is right for cards read through `get`, `list`, `get_by_correlation_key`, `create` and `update`;
     - `Card.model_json_schema()` is unchanged;
     - deleted cards are found only with `include_deleted`;
     - a `create_all` store gains both columns;
     - the repository command is normalized.

   Traps: `stage_runs` is plain `JSON`, so mutating it in place is not tracked. Assign a new dict.

2. **Guards** (`B/card_intake.py`, `B/github_sync.py`, `B/card_ops.py`, `B/routes_cards.py`, `B/mcp.py`):
   - **The error:** `InvestigationCardError(ValueError)` in `B/card_intake.py`, with `reason = "investigation_card"`.
   - **Where each guard goes:**
     - `update_card`: load with `store.get` and guard before `store.update` (`B/card_ops.py:362`);
     - `sync_card_github`: after `get_card` (`B/card_ops.py:389`);
     - `sync_card`: first line (`B/github_sync.py:302`) returns `{ok: False, skipped: "investigation_card"}`;
     - `maybe_sync`: first line (384) returns `None`;
     - `maybe_dispatch`: first line (663) returns `None`;
     - `dispatch_card`: first line (620) returns the refusal without writing;
     - `dispatch_stage`: first line (682), before project resolution at 692;
     - `start_sequence`: first line (728), before 729 and 750;
     - `apply_status`: after `get_by_correlation_key` (856) it ignores the card.
   - **Error mapping:** `routes_cards.update_card`, `routes_cards.sync_card_github` and `_stage_action` (`B/routes_cards.py:74`) map the error to 409 `{"detail": {"reason": "investigation_card"}}`. The MCP handlers return the same reason as a tool error.
   - **Check:** new `tests/test_investigation_guards.py`, with one test per guard. Each calls its path on an investigation card and asserts:
     - the REST status is 409, not 500;
     - nothing changed in `stage_runs`, status or tier;
     - no `Upstream` call and no provider call was made.

     It also asserts that `issue_import` never touches an investigation card. Then run `$PY/pytest -q tests/test_card_intake.py tests/test_card_stage_actions.py tests/test_github_sync.py tests/test_cards_api.py tests/test_mcp_board_tools.py tests/test_board_parity.py`, which must be unchanged and green.

   Traps:
   - `dispatch_card` does not call `refuse_reason`.
   - `start_sequence` refuses `no_tier` before writing; the guard must come first.
   - `update_card` does not load the card today.

3. **Close the executor** (`B/actions.py`):
   - **The function:** `_investigation_endpoint(path) -> bool`. It loops `unquote` until the value is stable, collapses `/+` to `/`, resolves `.` and `..` segments with `posixpath.normpath` on a leading `/`, lowercases, and returns true for `== "/api/investigations"` or a value starting `"/api/investigations/"`.
   - **Where it runs:** in `execute_action` (`B/actions.py:421`), before any request, over the primary step and every `follow_ups` step. On a match it returns `{ok: False, status_code: 403, reason: "investigations_via_executor", ...}` in the existing result shape. The `/api/actions/execute` route records it in the audit log as today.
   - **Check:** new `tests/test_executor_investigations.py`, which covers:
     - the paths `/api/investigations`, `/API/Investigations/x`, `/api/tasks/../investigations`, `/api//investigations`, `%2Fapi%2Finvestigations` and `/api/investigations%2F1`, as the primary step and as a follow-up, with no `Upstream` call;
     - an existing approve-review action, which still runs.

     Then run `$PY/pytest -q tests/test_actions*.py tests/test_routes_actions*.py`, or whichever files exist (`ls tests | grep -i action`).

   Traps: `is_safe_endpoint` already rejects schemes and hosts, so keep it. This check is an addition, not a replacement.

4. **The repository command, the proposal read, and card creation** (`B/git_providers.py`, `B/investigations.py` new, `B/routes_investigations.py` new, `B/main.py` or wherever routers are included, `B/routes_git_config.py` / `B/git_config_ops.py` for the audit):
   - **Provider calls**, on `HttpGitHubProvider` only:
     - `default_branch_head(repo) -> tuple[str, str]`: `GET /repos/{repo}`, then `GET /repos/{repo}/commits/{quote(branch, safe="")}`. A 409 or "Git Repository is empty" raises `EmptyRepository`.
     - `is_reachable_from_default(repo, branch, sha) -> bool`: `GET /repos/{repo}/compare/{quote(branch, safe="")}...{sha}`. `identical` or `behind` gives True; `ahead`, `diverged`, 404 or 422 give False; anything else raises.

     Both use the existing `_client` and `_TIMEOUT_SECONDS`. Bridge them into sync code with `run_sync` (`B/git_providers.py:153-167`).
   - **In `B/investigations.py`:**
     - `context(store, settings, repository_id, transport)`;
     - `create_card(store, audit, actor, request, repository_id)`, with the secret patterns and the title cap;
     - a `_repository(store, repository_id)` helper that never falls back: `None` uses the tenant default only for `context`; card creation requires an id.
   - **Routes,** in `B/routes_investigations.py`: `GET /api/investigations/context` and `POST /api/investigations/cards`. The router is included only when `settings.investigations != "off"` (read at app build, so tests rebuild the app).
   - **Audit:** a change to the repository command is recorded as `set_investigate_test_command`, with `sha256` hashes of the old and new values.
   - **Check:** new `tests/test_investigations_context.py`, using a fake GitHub transport, covers:
     - each refusal code;
     - a slash in a branch name;
     - an empty repository;
     - the secret-pattern refusals;
     - title truncation to 214 characters at most, with the full description kept;
     - audit rows that carry hashes, not text;
     - the router being absent when off.

5. **Dispatch and relays** (`B/investigations.py`, `B/routes_investigations.py`):
   - **`dispatch(...)`** implements checks 1–10 exactly as in Decisions, using the step 1 lock helpers.
   - **The HTTP helper:** `_aifactory(method, path, json, timeout, transport)`, built on `settings.aifactory_api_url`, `settings.token_for("aifactory")` and the `action_transport_dep` transport. It returns `(status, body | None)` and never raises on HTTP errors; timeouts and connection errors become `status 0`.
   - **The relay functions:** `lease`, `stop`, `get_run`, `logs` and `recover`. Each starts with the ownership check, and `get_run` and `stop` finish with the monotonic, `run_id`-conditioned record.
   - **Routes:** `POST /api/cards/{key}/actions/investigate`, `POST|GET /api/cards/{key}/investigate/{run_id}[/lease|/stop|/logs]` and `GET /api/cards/{key}/investigate`.
   - **Check:** new `tests/test_investigations_dispatch.py`, covering:
     - one test per refusal, each with no AIFactory call;
     - the three command cases;
     - the seven compare results;
     - a repository changed during compare, refused at reservation;
     - two concurrent dispatches, run in two threads through a `threading.Barrier`-gated fake compare, of which exactly one reaches AIFactory;
     - a card deleted during the AIFactory call, whose outcome is still recorded;
     - a slow read of run A after run B is reserved, which leaves B untouched;
     - every row of the outcome table;
     - relay ownership: a wrong `run_id`, a work card and a deleted card;
     - `pods_gone` never regressing;
     - a stop timeout recording `stopping`;
     - responses relayed field for field;
     - every write audited.

   Traps:
   - Use the store's lock pattern on both SQLite and PostgreSQL. The PostgreSQL suite runs in CI (`test.yml`); check how it gets its URL and run it locally if one is available.
   - Never write `stage_runs` outside the lock.

6. **Switch, credentials, tenancy and MCP** (`B/config.py`, `B/auth.py`, `B/mcp.py`, `B/routes_investigations.py`):
   - **The setting:** `investigations: Literal["off","on","drain"] = "off"`, next to `jira_enabled` (`B/config.py:217`).
   - **`require_investigation_write`:** a dependency that:
     - requires the write scope;
     - raises 403 `keys_required` when `get_keystore().open`;
     - raises 409 `multi_tenant_unsupported` when `settings.multi_tenant`;
     - raises 503 `investigations_draining` for create and dispatch when the switch is `drain`.

     A read dependency adds the tenancy check to `require_scope("read")`.
   - **MCP:**
     - `_authenticate` (`B/mcp.py:1009-1057`) returns, or records on the context, whether access came from `mcp_dev_open`;
     - the eight tools get `MCP_TOOLS`, `TOOL_SCOPES` (writes `WRITE`, reads `READ`) and `_TOOL_HANDLERS` entries that call `B/investigations.py`;
     - `tools/list` (`B/mcp.py:1544`) filters by the switch;
     - `_dispatch_tool` refuses investigation tools when the switch is off (with "unknown tool") and applies the drain, dev-open and tenancy rules, so a direct call matches `tools/list`.
   - **Check:** new `tests/test_investigations_auth.py`, covering:
     - a read key gets 403 on all four writes, on REST and MCP;
     - REST dev-open and MCP dev-open with no keys both give `keys_required`;
     - the legacy `mcp_secret` is accepted;
     - with `multi_tenant` on, every route refuses;
     - `tools/list` and direct `tools/call` behave per state in `off`, `on` and `drain`.

     Then the full suite: `$PY/pytest -q tests`.

7. **Docs and deploy** (`openapi.yaml`, `techdocs/dependencies.md:93-102`, `.env.example`, `charts/cfactory/values.yaml`, `charts/cfactory/templates/configmap.yaml`, `docs/guides/token-gated-api.md`):
   - add the new routes and `investigate_test_command` to `openapi.yaml`, leaving the Card schemas unchanged;
   - document `CFACTORY_INVESTIGATIONS`;
   - add the Helm value `investigations: off`, mapped to the env.

   **Check:** `just helm-template | grep CFACTORY_INVESTIGATIONS`, `just helm-lint`, and `python -c 'import yaml; yaml.safe_load(open("openapi.yaml"))'`. If `openapi-spec-validator` is installable, run it on `openapi.yaml`.

8. **Gates (lead):**
   - the full suite;
   - ruff check and format;
   - `python scripts/ratchet_lint.py --base origin/dev --package apps/backend/cfactory --package scripts`, and the same with `--tool mypy`;
   - `alembic heads` shows one head;
   - the planning-card conformance script run locally against the hub checkout (`../Factory/scripts/check_planning_card_conformance.py`, with the arguments the workflow uses), which must pass with no Card schema change.

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
  2. Wait until every card's `stage_runs.investigate` reads `pods_gone: true`, and AIFactory shows no live run for each investigated project.
  3. Set it to `off`.

  Existing investigation cards stay inert, because the guards do not depend on the switch.
- **Revert the code:** revert the PR. Both migrations only add nullable or defaulted columns, and their `downgrade` drops them.
  - Downgrading drops the investigation marker, so first delete every investigation card. They would otherwise become ordinary no-tier cards.
  - The executor refusal goes with the revert, which reopens the existing gap described in the spec.

---
status: approved
issue: 1959
repo-issue: olafkfreund/Factory#1959
spec: spec/2026-11-14-1959-copilot-gateway-flag.md
coders:
  1: codex   # small, security-sensitive logic: credential routing and the no-fallback rule
  2: codex   # the tests pin step 1's behaviour; same context, same agent
  3: coder   # routine UI wording and schema field
  4: coder   # routine docs rows
reviewers:
  - codex    # independent read of the credential paths
  - agy      # second independent read; found the unsaved-provider UI case at spec review
---

# Plan: copilot can call the model gateway, behind a flag that defaults off

## Approved decisions (from the spec)

- Two new settings, `CFACTORY_`-prefixed only, no bare alias:
  `model_gateway_url: str = ""` (gateway origin, no path) and
  `model_gateway_token: str | None` with `repr=False`.
- Both are stripped of surrounding whitespace at use; the URL also loses a
  trailing `/`. "Flag on" = URL non-empty after stripping. An empty token
  after stripping counts as missing.
- Chat, flag on and provider in `_OPENAI_PROVIDERS`: POST
  `{url}/v1/chat/completions` with `Authorization: Bearer <gateway token>`.
  `ollama_api_key` and `ollama_cloud_base_url` are not read. Token missing:
  the runner raises `RuntimeError("model gateway token is not configured")`
  when called and sends nothing. Gateway errors propagate; never fall back
  to the provider.
- Chat, flag off or provider `claude`: unchanged.
- Status, flag on, **any** provider: exactly one request,
  `GET {url}/healthz`, 10 s timeout, no `Authorization`; success is
  `resp.raise_for_status()` passing, the body is not parsed. Result:
  `{provider, model, gateway: True, has_key: bool(token), reachable}`; on
  failure `reachable: False, error: "gateway probe failed"` and a logged
  exception. No `models`, no `base_url`. `provider` is never rewritten.
  `OLLAMA_API_KEY` is sent nowhere.
- Status, flag off: unchanged.
- `reachable` with the flag on means the gateway process answers, nothing
  more. Said in the docstring and in the UI.
- UI: when `settings.gateway` is true the pill reads "Model gateway
  responding" / "Model gateway unreachable", followed by the hint "Health
  check only. To test the full path, save this provider and ask the copilot
  a question." (the row shows for a provider selected but not yet saved, and
  chat follows the saved one); the
  missing-key hint reads "No CFACTORY_MODEL_GATEWAY_TOKEN configured — set it
  in the deployment."
- No deployment change. The deployed flag stays off.
- Turning it on later (owner, not this PR; goes in the PR body): the gateway
  must be configured and have egress (factory-gitops spec
  `2026-11-14-1959-gateway-stage1-secretref`); set both
  `CFACTORY_MODEL_GATEWAY_URL` and `CFACTORY_MODEL_GATEWAY_TOKEN` on the
  Deployment (a restart: settings are read once); save provider `ollama` and
  a model the upstream serves in Settings (default is `claude`, and
  `copilot-settings.json` overrides env). Turning it off: unset the URL.
- Known and unchanged: a failed chat call surfaces as HTTP 500 without the
  cause (`routes_copilot.py` catches nothing), and the Settings connection
  row only shows for `ollama`, not `openai` / `openai_compatible`.

Commands, from the repo root. A fresh worktree has no venv or node_modules:
use the main clone's
(`/mnt/code/Source-home/GitHub/CFactory/apps/backend/.venv/bin/{pytest,ruff}`),
never install into the worktree.

- Tests: `PYTHONPATH=apps/backend <venv>/bin/pytest -q tests/test_copilot_provider.py tests/test_copilot.py tests/test_settings_copilot.py`
- Lint: `<venv>/bin/ruff check --config ruff.toml <files>` and
  `ruff format --check --config ruff.toml <files>`. Never `--fix` or format
  files you did not change.

Implementers never stage, commit, push or switch branches.

## Steps

1. Backend.
   - `apps/backend/cfactory/config.py`, directly after the `ollama_api_key`
     field (~l.237-241): add the two fields with a comment block in the
     style of the neighbours: what the flag does, that the URL is the origin
     without `/v1` (unlike `ollama_cloud_base_url`), and why there is no bare
     alias (point at the `github_token` comment, ~l.110).
   - `apps/backend/cfactory/copilot/service.py`:
     - add `_gateway(settings) -> tuple[str, str]` returning the stripped URL
       (also `rstrip("/")`) and the stripped token (`""` when unset).
     - `make_runner` (~l.138): when provider is in `_OPENAI_PROVIDERS` and the
       gateway URL is non-empty: token set → `_openai_compatible_runner(
       f"{url}/v1", token, settings.copilot_model)`; token empty → a runner
       that raises the `RuntimeError` above. Otherwise unchanged.
     - `provider_status` (~l.149): when the gateway URL is non-empty, return
       the gateway status described above before any provider logic. Update
       the docstring. Keep the existing `except Exception` + `logger.exception`
       shape; the log line must not include the token. Use
       `resp.raise_for_status()` and do not call `resp.json()`.
   - Verify: lint both files.
   - Traps: do not use `SecretStr` (see the `Settings` docstring: it sends
     `Bearer **********`). Do not log or return the token. `Settings` is cached (`get_settings`); do not add another cache.
     `routes_copilot.py` needs no change.

2. `tests/test_copilot_provider.py`: add the cases below, in the file's
   existing style (`httpx.MockTransport` behind a monkeypatched `httpx.post` /
   `httpx.get`; `Settings(...)` built explicitly so ambient config cannot leak:
   every new test passes `_env_file=None` (the class reads `.env`, which
   `monkeypatch.delenv` does not touch) and sets or clears both gateway
   settings and `ollama_api_key` on purpose).
   - flag off: `make_runner` still passes `ollama_cloud_base_url` and
     `ollama_api_key`.
   - flag on, provider `ollama`, token set: URL is
     `{url}/v1/chat/completions`, header is `Bearer <gateway token>`; the same
     with a trailing `/` and surrounding whitespace on the URL and a trailing
     newline on the token; the `ollama_api_key` marker is in no header.
   - flag on, token `None`, `""` and `"  "`: calling the runner raises
     `RuntimeError`; the transport saw no request.
   - flag on, gateway returns 503: the runner raises `httpx.HTTPStatusError`
     and the transport saw exactly one request, to the gateway host.
   - flag on, provider `claude`: the Claude runner branch is taken.
   - URL `"   "`: flag off (provider path as today).
   - `MODEL_GATEWAY_URL` and `MODEL_GATEWAY_TOKEN` set in the environment
     (`monkeypatch.setenv`) without the prefix: `Settings()` has the flag off.
   - status, flag on, for provider `ollama` and for provider `claude` with an
     `ollama_api_key` marker: exactly one GET, path `/healthz`, no
     `Authorization` header; result has `gateway is True`, `reachable is
     True`, no `models`, no `base_url`; the mocked `/healthz` answers 200
     with an empty body (it must not be parsed); neither the token nor the marker
     appears in `repr(result)`.
   - status, flag on, `/healthz` → 500, and transport raising
     `httpx.ConnectError`: `reachable is False`, `error` set, no secret in
     the result.
   - `repr(Settings(model_gateway_token="tok-123"))` does not contain
     `tok-123`.
   - Verify: the test command above passes; lint the test file.
   - Traps: `Settings` reads `.env` and the process environment; a test that
     relies on a default passes `_env_file=None` **and** clears
     `CFACTORY_MODEL_GATEWAY_URL` / `CFACTORY_MODEL_GATEWAY_TOKEN` with
     `monkeypatch.delenv(..., raising=False)`. The existing tests in the file
     that expect the direct path get the same treatment if they would break
     under a developer's `.env`.

3. Frontend and API description.
   - `apps/frontend-web/src/api.ts`, `CopilotSettingsSchema` (~l.600): add
     `gateway: z.boolean().optional(), // true = OpenAI-compatible chat is routed through the model gateway`.
   - `apps/frontend-web/src/SettingsView.tsx` (~l.138-147, the `set-conn`
     block): wording per the decisions above, switched on `settings?.gateway`.
     No new component, no new CSS.
   - `openapi.yaml`: the two response schemas that list `has_key` /
     `reachable` (~l.287 and ~l.320): add `gateway: { type: boolean }`.
   - `apps/frontend-web/src/api.test.ts`: one case that
     `CopilotSettingsSchema` parses a gateway status
     (`{provider, model, providers, gateway: true, has_key: true, reachable: true}`)
     and keeps `gateway`.
   - Verify: `python -c "import yaml; yaml.safe_load(open('openapi.yaml'))"`.
     The frontend build and tests are run by the lead (below); the coder does
     not `npm install`.
   - Traps: `src/apiContract.test.ts` deliberately excludes the copilot
     settings; leave it alone.

4. Docs.
   - `.env.example`, Copilot block (after `#CFACTORY_OLLAMA_API_KEY=`, ~l.75):
     two commented lines with one comment line each
     (`#CFACTORY_MODEL_GATEWAY_URL=`, `#CFACTORY_MODEL_GATEWAY_TOKEN=`).
   - `docs/dev/environment-reference.md`, after the `CFACTORY_OLLAMA_API_KEY`
     row (~l.50): one row per variable, same column layout.
   - Verify: `grep -c MODEL_GATEWAY .env.example docs/dev/environment-reference.md`
     shows both variables in both files.
   - Traps: none.

## Tests

- `PYTHONPATH=apps/backend <venv>/bin/pytest -q tests` green (whole backend
  suite, once, by the lead).
- `ruff check` and `ruff format --check` clean on touched Python files;
  `scripts/ratchet_lint.py` as CI runs it, if it can run locally.
- Frontend, by the lead, in `apps/frontend-web` with `node_modules`
  symlinked from the main clone and the symlink removed afterwards:
  `npm run build` (tsc + vite) and `npm test` (vitest).
- CI green on the PR.

## Rollback

Revert the PR. With the flag unset the change is inert, so there is nothing to
roll back in a deployment.

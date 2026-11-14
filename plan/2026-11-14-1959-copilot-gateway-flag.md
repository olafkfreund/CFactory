---
status: draft
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
  `GET {url}/healthz`, 10 s timeout, no `Authorization`. Result:
  `{provider, model, gateway: True, has_key: bool(token), reachable}`; on
  failure `reachable: False, error: "gateway probe failed"` and a logged
  exception. No `models`, no `base_url`. `provider` is never rewritten.
  `OLLAMA_API_KEY` is sent nowhere.
- Status, flag off: unchanged.
- `reachable` with the flag on means the gateway process answers, nothing
  more. Said in the docstring and in the UI.
- UI: when `settings.gateway` is true the pill reads "Model gateway
  responding" / "Model gateway unreachable", followed by the hint "Health
  check only — ask the copilot a question to test the full path."; the
  missing-key hint reads "No CFACTORY_MODEL_GATEWAY_TOKEN configured — set it
  in the deployment."
- No deployment change. The deployed flag stays off.

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
       shape; the log line must not include the token.
   - Verify: lint both files.
   - Traps: do not use `SecretStr` (see the `Settings` docstring: it sends
     `Bearer **********`). Do not log or return the URL's credentials or the
     token. `Settings` is cached (`get_settings`); do not add another cache.
     `routes_copilot.py` needs no change.

2. `tests/test_copilot_provider.py`: add the cases below, in the file's
   existing style (`httpx.MockTransport` behind a monkeypatched `httpx.post` /
   `httpx.get`; `Settings(...)` built explicitly so ambient env cannot leak —
   pass `ollama_api_key=None` or a marker value on purpose).
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
     True`, no `models`, no `base_url`; neither the token nor the marker
     appears in `repr(result)`.
   - status, flag on, `/healthz` → 500, and transport raising
     `httpx.ConnectError`: `reachable is False`, `error` set, no secret in
     the result.
   - `repr(Settings(model_gateway_token="tok-123"))` does not contain
     `tok-123`.
   - Verify: the test command above passes; lint the test file.
   - Traps: `Settings` reads `.env` and the process environment; a test that
     relies on a default must clear `CFACTORY_MODEL_GATEWAY_URL` /
     `CFACTORY_MODEL_GATEWAY_TOKEN` with `monkeypatch.delenv(..., raising=False)`.

3. Frontend and API description.
   - `apps/frontend-web/src/api.ts`, `CopilotSettingsSchema` (~l.600): add
     `gateway: z.boolean().optional(), // true = OpenAI-compatible chat is routed through the model gateway`.
   - `apps/frontend-web/src/SettingsView.tsx` (~l.138-147, the `set-conn`
     block): wording per the decisions above, switched on `settings?.gateway`.
     No new component, no new CSS.
   - `openapi.yaml`: the two response schemas that list `has_key` /
     `reachable` (~l.287 and ~l.320): add `gateway: { type: boolean }`.
   - Verify: in `apps/frontend-web`, `npx tsc --noEmit` and `npx vitest run`
     (node_modules: symlink or use the main clone's; do not `npm install` in
     the worktree); `python -c "import yaml,sys; yaml.safe_load(open('openapi.yaml'))"`.
   - Traps: `src/apiContract.test.ts` pins contract fields; if it lists the
     copilot settings fields, add `gateway` there too.

4. Docs.
   - `.env.example`, Copilot block (after `#CFACTORY_OLLAMA_API_KEY=`, ~l.75):
     two commented lines with one comment line each
     (`#CFACTORY_MODEL_GATEWAY_URL=`, `#CFACTORY_MODEL_GATEWAY_TOKEN=`).
   - `docs/dev/environment-reference.md`, after the `CFACTORY_OLLAMA_API_KEY`
     row (~l.50): one row per variable, same column layout.
   - Verify: `grep -n MODEL_GATEWAY .env.example docs/dev/environment-reference.md`
     shows four lines.
   - Traps: none.

## Tests

- `PYTHONPATH=apps/backend <venv>/bin/pytest -q tests` green (whole backend
  suite, once, by the lead).
- `ruff check` and `ruff format --check` clean on touched Python files;
  `scripts/ratchet_lint.py` as CI runs it, if it can run locally.
- Frontend: `npx tsc --noEmit`, `npx vitest run`.
- CI green on the PR.

## Rollback

Revert the PR. With the flag unset the change is inert, so there is nothing to
roll back in a deployment.

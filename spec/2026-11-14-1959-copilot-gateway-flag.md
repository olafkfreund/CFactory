---
status: draft
issue: 1959
repo-issue: olafkfreund/Factory#1959
intent: intent/2026-11-14-1959-copilot-gateway-flag.md
---

# Spec: copilot can call the model gateway, behind a flag that defaults off

## Design

**Settings** (`apps/backend/cfactory/config.py`, next to `ollama_api_key`):

- `model_gateway_url: str = ""` — env `CFACTORY_MODEL_GATEWAY_URL`. The
  gateway origin with no path, e.g.
  `http://model-gateway.gateway.svc.cluster.local:8080`. Empty means off.
  A trailing `/` is stripped at use.
- `model_gateway_token: str | None = Field(default=None, repr=False)` — env
  `CFACTORY_MODEL_GATEWAY_TOKEN`. The gateway client token.
- Only the `CFACTORY_`-prefixed names, no bare alias: an ambient variable must
  not turn the flag on (the `github_token` precedent in the same file).

"Gateway on" means `model_gateway_url` is non-empty **and** the copilot
provider is OpenAI-compatible (`_OPENAI_PROVIDERS` in `copilot/service.py`).

**Chat** (`make_runner`, `copilot/service.py`), gateway on:

- Token set: return
  `_openai_compatible_runner(f"{url}/v1", model_gateway_token, copilot_model)`.
  The existing runner then posts `{url}/v1/chat/completions` with
  `Authorization: Bearer <client token>` and `stream: False`, which is the
  only route the gateway serves. `ollama_api_key` and `ollama_cloud_base_url`
  are not read.
- Token missing or empty: return a runner that raises `RuntimeError("model
  gateway token is not configured")` when called. No request is sent. This is
  call-time, not import-time, so the app still starts and `/api/settings`
  still answers.
- Any gateway error (401, 502, 503, timeout) propagates from
  `resp.raise_for_status()` exactly as a provider error does today. There is
  no fallback to the direct provider.

Gateway off, or provider `claude`: `make_runner` is unchanged.

**Status** (`provider_status`, same file):

- `model_gateway_url` set and provider OpenAI-compatible: no provider call.
  Return `{provider, model, gateway: True, has_key: bool(model_gateway_token),
  reachable}` where `reachable` is whether `GET {url}/healthz` returns 200
  (10 s timeout, no `Authorization` header; the gateway's `/healthz` takes
  none). No `models`, no `base_url` (the in-cluster name is of no use to a
  browser). On failure: `reachable: False`, `error: "gateway probe failed"`,
  logged like today's probe failure.
- `model_gateway_url` set and provider `claude`: return
  `{provider, model, reachable: None}`. Today this branch probes Ollama Cloud
  with `OLLAMA_API_KEY` when a key is set; with the flag on it must not.
- `model_gateway_url` empty: unchanged.
- `provider` is always the selected value, never rewritten.

`reachable: True` here means the gateway process answers. It is true for an
unconfigured gateway that would answer chat 503. Stated in the docstring and
in the UI wording below; a real check is one chat call.

**UI** (`apps/frontend-web/src/api.ts`, `SettingsView.tsx`): add
`gateway: z.boolean().optional()` to the copilot settings schema. When
`settings.gateway` is true the status pill reads "Model gateway reachable" /
"Model gateway unreachable" instead of "Ollama Cloud …", and the missing-key
hint reads "No CFACTORY_MODEL_GATEWAY_TOKEN configured — set it in the
deployment." Nothing else changes.

**Docs**: two commented lines in `.env.example` under the Copilot block.

## Alternatives rejected

- **Point `CFACTORY_OLLAMA_CLOUD_BASE_URL` at the gateway and put the client
  token in `OLLAMA_API_KEY`**: no code, but the status probe would call
  `/v1/models` (404, "unreachable" forever), and the provider key variable
  would hold a gateway token, which makes the epic's AC-1 check (no provider
  key in env) unreadable.
- **A separate boolean plus a URL**: two settings that can disagree. The URL
  being set is the flag.
- **Fall back to the direct call when the gateway fails**: bypasses the
  gateway silently; the AIFactory helper (`providers/_gateway.py`) rejects
  it for the same reason.
- **Probe with a real chat call**: spends tokens on every Settings page load.
- **Raise in `make_runner` when the token is missing**: `get_copilot()` would
  fail for every request, including ones the fake runner serves in tests.
- **Route the Claude provider too**: Plane B; blocked on the epic's open
  bench question.

## Risks

- Flag turned on before the gateway is configured or has egress: every copilot
  question fails (503, or 502 after the gateway's upstream timeout while
  CFactory's own 60 s timeout may fire first). Visible, not silent; turning the
  flag off restores today's path. Order of enablement is in the gitops spec.
- The gateway buffers responses (no streaming). CFactory already sends
  `stream: False`, so nothing is lost.
- A runtime switch of provider via the Settings UI clears the cached copilot
  (`reset_copilot`), so the gateway decision is re-read from settings; the URL
  and token themselves are env-only and need a restart to change.
- `GET /api/copilot/provider` changes shape only when the flag is on
  (`gateway` added, `models`/`base_url` absent). Both were already optional in
  the frontend schema.

## Verification

- `tests/test_copilot_provider.py`, new cases:
  - flag off: `make_runner` passes `ollama_cloud_base_url` and
    `ollama_api_key` as today.
  - flag on, provider `ollama`, token set: the request goes to
    `{url}/v1/chat/completions` with `Bearer <client token>`; a trailing `/`
    on the URL gives the same request; `ollama_api_key` appears in no header.
  - flag on, token missing: calling the runner raises, and no HTTP request is
    made.
  - flag on, provider `claude`: the Claude runner is selected.
  - status, flag on, provider `ollama`: one GET to `{url}/healthz`, no
    `Authorization` header, result has `gateway: True`, no `models`, no
    `base_url`, and neither token nor provider key anywhere in the result.
  - status, flag on, provider `claude`, `ollama_api_key` set: no HTTP request,
    `reachable is None`.
  - `repr(Settings(model_gateway_token="x"))` does not contain the token.
- `just test` green; ruff clean on touched files; `npm run build` and the
  frontend tests green.
- No live check: the deployed flag stays off.

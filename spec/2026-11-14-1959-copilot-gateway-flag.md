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
  Surrounding whitespace and a trailing `/` are stripped at use, so a
  whitespace-only value is off. A value ending in `/v1` is a
  misconfiguration: the health check and chat both 404, which the status
  shows as unreachable.
- `model_gateway_token: str | None = Field(default=None, repr=False)` — env
  `CFACTORY_MODEL_GATEWAY_TOKEN`. The gateway client token. Surrounding
  whitespace is stripped at use (a trailing newline from a Secret would
  otherwise be an invalid header); empty after stripping counts as missing.
- Only the `CFACTORY_`-prefixed names, no bare alias: an ambient variable must
  not turn the flag on (the `github_token` precedent in the same file).

"Flag on" means `model_gateway_url` is non-empty after stripping. Chat uses
the gateway when the flag is on **and** the copilot provider is
OpenAI-compatible (`_OPENAI_PROVIDERS` in `copilot/service.py`).

**Chat** (`make_runner`, `copilot/service.py`), flag on and provider
OpenAI-compatible:

- Token set: return
  `_openai_compatible_runner(f"{url}/v1", model_gateway_token, copilot_model)`.
  The existing runner then posts `{url}/v1/chat/completions` with
  `Authorization: Bearer <client token>` and `stream: False`, which is the
  only route the gateway serves. `ollama_api_key` and `ollama_cloud_base_url`
  are not read.
- Token missing or empty: return a runner that raises `RuntimeError("model
  gateway token is not configured")` when called. No request is sent. This is
  call-time, so the failure is on the chat request with a message that names
  the cause, and `/api/settings` keeps answering.
- Any gateway error (401, 502, 503, timeout) propagates from
  `resp.raise_for_status()` exactly as a provider error does today. There is
  no fallback to the direct provider.

Flag off, or provider `claude`: `make_runner` is unchanged.

**Status** (`provider_status`, same file):

- Flag on, any provider: no provider call. Return
  `{provider, model, gateway: True, has_key: bool(token), reachable}` where
  `reachable` is whether `GET {url}/healthz` returns 200 (10 s timeout, no
  `Authorization` header; the gateway's `/healthz` takes none). No `models`,
  no `base_url` (the in-cluster name is of no use to a browser). On failure:
  `reachable: False`, `error: "gateway probe failed"`, logged like today's
  probe failure.
  The same answer for `claude` on purpose: the Settings page shows the
  connection row for the provider the user has *selected but not yet saved*
  (`SettingsView.tsx`, `ollama` is derived from local state), so a status
  that changed shape with the saved provider would show "Ollama Cloud
  unreachable" while the user checks the gateway before switching. It also
  means that with the flag on, `OLLAMA_API_KEY` is sent nowhere; today the
  `claude` branch probes Ollama Cloud with it whenever a key is set.
- `model_gateway_url` empty: unchanged.
- `provider` is always the selected value, never rewritten.

`reachable: True` here means the gateway process answers. It is true for an
unconfigured gateway that would answer chat 503, and for a wrong client
token. Stated in the docstring and shown in the UI (below); a real check is
one chat call.

**UI** (`apps/frontend-web/src/api.ts`, `SettingsView.tsx`): add
`gateway: z.boolean().optional()` to the copilot settings schema. When
`settings.gateway` is true the status pill reads "Model gateway responding" /
"Model gateway unreachable" instead of "Ollama Cloud …", followed by the hint
"Health check only — ask the copilot a question to test the full path." The
missing-key hint reads "No CFACTORY_MODEL_GATEWAY_TOKEN configured — set it
in the deployment." Nothing else changes.

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
- **Raise in `make_runner` when the token is missing**: the error would come
  out of the FastAPI dependency (`get_copilot`) rather than the chat call,
  and a provider switch in the UI (`reset_copilot`) would move it around.
- **Route the Claude provider too**: Plane B; blocked on the epic's open
  bench question.

## Risks

- Flag turned on before the gateway is configured or has egress: every copilot
  question fails (503, or 502 after the gateway's upstream timeout while
  CFactory's own 60 s timeout may fire first). Visible, not silent; turning the
  flag off restores today's path. Order of enablement is in the gitops spec.
- CFactory's chat timeout is 60 s and the gateway's upstream timeout is
  120 s. A slow model answer between the two fails in CFactory first. Same
  limit as the direct path today; not changed here.
- Turning the flag on needs more than the two settings: the saved copilot
  provider must be `ollama` (default is `claude`, and `copilot-settings.json`
  on the PVC overrides env), with a model the upstream serves. Listed in the
  gitops spec's owner steps.
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
  - flag on, token missing, empty or whitespace: calling the runner raises,
    and no HTTP request is made.
  - flag on, gateway answers 503: the runner raises and exactly one request
    was made, to the gateway (no retry against the provider).
  - URL of only whitespace: flag off.
  - `MODEL_GATEWAY_URL` / `MODEL_GATEWAY_TOKEN` without the `CFACTORY_`
    prefix in the environment: flag off.
  - flag on, provider `claude`: the Claude runner is selected.
  - status, flag on, provider `ollama` and provider `claude` (with
    `ollama_api_key` set): exactly one GET, to `{url}/healthz`, no
    `Authorization` header; result has `gateway: True`, no `models`, no
    `base_url`, and neither token nor provider key anywhere in the result.
  - status, flag on, `/healthz` fails (500, and a connection error):
    `reachable is False`, `error` set, no secret in the result.
  - `repr(Settings(model_gateway_token="x"))` does not contain the token.
- `just test` green; ruff clean on touched files; `npm run build` and the
  frontend tests green.
- No live check: the deployed flag stays off.

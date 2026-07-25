---
layout: default
title: Environment reference
permalink: /dev/environment-reference/
---

# Environment reference

This is the complete reference for every environment variable, flag and
operational parameter CFactory reads — backend, the copilot's external secret,
the frontend build-time bundle, and the frontend container runtime. A fresh
operator needs nothing beyond this page plus the [`.env.example`](https://github.com/olafkfreund/CFactory/blob/main/.env.example)
at the repo root.

Almost every backend value is a field on the `Settings` class in
`apps/backend/cfactory/config.py` (`env_prefix="CFACTORY_"`, `.env` file
support), with one exception — `CFACTORY_STALL_DEADLINE_SECONDS` is read
directly in `store.py`. Two backend values also accept a bare, un-prefixed
alias so they can share the fleet-wide Ollama secret.

Conventions in the tables below:

- **Required?** — `no` means the built-in default is fine everywhere; *in hosted*
  means it must be set for any shared/multi-user deployment; *if provider is X*
  means it is only needed for a particular copilot provider.
- **Server-side only** secrets are never sent to the browser.

## Backend (`CFACTORY_*`)

| Variable | Default | Required? | Purpose |
|---|---|---|---|
| `CFACTORY_BACKEND_PORT` | `3111` | no | API server port. |
| `CFACTORY_FRONTEND_PORT` | `3110` | no | Cockpit UI port / CORS origin. |
| `CFACTORY_WORKSPACE_ROOT` | `~/.cfactory` | no | Local state root — persisted endpoint/copilot override JSON and the local SQLite store. |
| `CFACTORY_AIFACTORY_API_URL` | `http://localhost:3101` | no | AIFactory (Act) upstream REST base. Editable at runtime from the Services view. |
| `CFACTORY_PFACTORY_API_URL` | `http://localhost:3105` | no | PFactory (Plan) upstream REST base. Editable at runtime. |
| `CFACTORY_TFACTORY_API_URL` | `http://localhost:3103` | no | TFactory (Verify) upstream REST base. Editable at runtime. |
| `CFACTORY_OBSERVE_API_URL` | `http://localhost:5080` | no | OpenObserve health-probe base for the Services reachability view (not a PARR factory; never polled/hydrated). In-cluster: `http://observe.factory.svc.cluster.local:5080`. |
| `CFACTORY_UPSTREAM_TOKEN` | _(unset)_ | in hosted | Shared bearer token sent as `Authorization: Bearer <token>` on every adapter call, live-progress poll and upstream WS subscription. Leave unset only for local dev where the factories run `APP_DISABLE_AUTH=true`. Server-side only. |
| `CFACTORY_AIFACTORY_TOKEN` | _(unset; falls back to `CFACTORY_UPSTREAM_TOKEN`)_ | no | Service token for AIFactory's live-agent console WebSocket. Server-side only. |
| `CFACTORY_INTAKE_PROJECT_ID` | _(unset)_ | if any `low`/`medium` card is promoted | AIFactory project id a dispatched planning card is built into (RFC-0019 §3.2). AIFactory's `/api/tasks/from-issue` requires a `project_id` and a card carries none, so it is deployment config. Unset = a `low`/`medium` card promoted to `ready` is moved to `blocked` with that reason (loud, never silently accepted); `hard` cards route to PFactory and need no project. Recommended: the sacrificial demo project — hosted runs `5d78d4b9-35f9-4445-92c1-78f3ff60a494` (`aifactory-demo`) — because an autonomous build writes real code. No dedicated Helm key; inject via `config.extraEnv`. See [the planning-board guide](../guides/planning-board.md#cfactory_intake_project_id-in-full). |
| `CFACTORY_DATABASE_URL` | _(unset)_ | no | Postgres connection string for the WorkItem correlation store. Unset = local SQLite under the workspace root. Also stores the RFC-0019 planning cards, in their own table. |
| `CFACTORY_SUBSCRIBE_UPSTREAMS` | `false` | no | On: connect to each upstream `/ws/events` feed at startup. Off: no upstream WS (avoids reconnect loops against down services). |
| `CFACTORY_LIVE_PROGRESS` | `false` | no | On: poll PFactory/TFactory + subscribe AIFactory progress and broadcast `{type:"progress"}`. Off: no live-progress. |
| `CFACTORY_POLL_INTERVAL_SECONDS` | `3.0` | no | Live-progress poll interval, seconds. Only relevant when `CFACTORY_LIVE_PROGRESS` is on. |
| `CFACTORY_STALL_DEADLINE_SECONDS` | `900.0` | no | Idle budget (seconds) before an active, non-terminal stage is flagged stalled and pruned from the board. Values `<= 0` or unparseable fall back to the default. Read directly in `store.py`. |
| `CFACTORY_COPILOT_MODEL` | `claude-opus-4-8` | no | Copilot model id; meaning depends on `CFACTORY_COPILOT_PROVIDER`. Editable at runtime from the Settings view. |
| `CFACTORY_COPILOT_PROVIDER` | `claude` | no | Copilot LLM provider: `claude` (Claude Agent SDK, reads `ANTHROPIC_API_KEY`) or `ollama`/`openai_compatible` (any OpenAI-compatible chat endpoint). |
| `CFACTORY_OLLAMA_CLOUD_BASE_URL` | `https://ollama.com/v1` | no | OpenAI-compatible copilot base URL (includes `/v1`). Also accepts the bare `OLLAMA_CLOUD_BASE_URL`. Only used when provider is not `claude`. |
| `CFACTORY_OLLAMA_API_KEY` | _(unset)_ | if provider is ollama | Bearer key for the OpenAI-compatible copilot endpoint. Also accepts the bare `OLLAMA_API_KEY` (shared factory secret). Server-side only. |
| `CFACTORY_API_KEYS` | _(unset)_ | in hosted | Scoped API keys `<key>:read,write;<key2>:read`. Unset = auth OPEN (single-user local mode); set = requests must carry a known key with the required scope. Also gates `/mcp` per declared tool scope, so a `read` key can enumerate the planning backlog and never change it (RFC-0019 Phase 2a). Recommended: one `read` key per watcher, one `read,write` key per agent that plans — never one shared key. Server-side only. |
| `CFACTORY_MCP_SECRET` | _(unset)_ | in hosted | LEGACY full-scope bearer for the MCP transport (`POST /mcp`) — a caller presenting it holds `read` and `write`, including every board write tool. Still the supported prod credential; scoped `CFACTORY_API_KEYS` work alongside it and are preferred for new clients. A wrong token against a configured server is a loud 401. Server-side only. |
| `CFACTORY_MCP_DEV_OPEN` | `false` | no | Explicit local-dev opt-in that reopens `/mcp` when NO credential is configured. Unconfigured otherwise means DENY (RFC-0019 Phase 2a) — never set this in a hosted deploy. Ignored once `CFACTORY_MCP_SECRET` or `CFACTORY_API_KEYS` is set. |
| `CFACTORY_PUBLIC_API_URL` | _(unset)_ | no | Public base URL of the token-gated API shown on `/settings/token` for editor/external clients. Display only. |
| `CFACTORY_MULTI_TENANT` | `false` | no | On: resolve tenant per request from the `X-Tenant-Id` header (hosted, injected by oauth2-proxy from the Keycloak tenant claim). Off: single `default` tenant. Planning cards are scoped the same way, and `card_key` is unique per tenant — two tenants may each hold an `FCT-1`. |
| `CFACTORY_GITHUB_TOKEN` | _(unset)_ | no | Enables GitHub card <-> issue sync (RFC-0019 section 3.5). Unset = sync OFF: no network call on a card write, no issue opened. The bare `GITHUB_TOKEN`/`GH_TOKEN` are deliberately **not** accepted — this credential opens issues in a real repo, so an ambient `gh` login must not switch the feature on. Server-side only. |
| `CFACTORY_GITHUB_REPO` | _(unset)_ | no | `owner/repo` a `ready` card opens its issue in. Unset = cards can only *adopt* an existing issue (set the card's `issue_ref`), never create one. |
| `CFACTORY_GITHUB_API_URL` | `https://api.github.com` | no | GitHub API base. Override for GitHub Enterprise. |
| `CFACTORY_AUDIT_HMAC_SECRET` | dev secret (`dev-insecure-...`) | in hosted | HMAC secret anchoring the tamper-evident audit chain. MUST be overridden in any hosted/shared deploy (API keys or multi-tenant set) — the default is a clearly-labelled dev value and startup hard-warns if it is left in place in a non-local posture. Server-side only. |

## Copilot external secret

Read by the Claude Agent SDK — not by CFactory code — when
`CFACTORY_COPILOT_PROVIDER=claude`:

| Variable | Default | Required? | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | _(unset)_ | if provider is claude | Claude Agent SDK credential for the copilot. Server-side only. |

## Frontend (`VITE_*`, build-time)

`VITE_*` variables are read at **build time** by Vite and inlined into the
static bundle (`import.meta.env`) — they are not runtime container env. Set them
as build args / build-environment when running `npm run build` (or in the
frontend image build). Each has a hard-coded fallback, so all are optional.

| Variable | Default (fallback) | Purpose |
|---|---|---|
| `VITE_OBSERVE_URL` | `https://observe.freundcloud.org.uk` (set empty to hide) | OpenObserve dashboard URL linked from the Services view / header. Set to an empty string at build time to hide the link entirely. |
| `VITE_PFACTORY_URL` | `https://pfactory.freundcloud.org.uk` | PFactory (Plan) portal link target in the portal switcher / stage rail. |
| `VITE_AIFACTORY_URL` | `https://aifactory.freundcloud.org.uk` | AIFactory (Build) portal link target. |
| `VITE_TFACTORY_URL` | `https://tfactory.freundcloud.org.uk` | TFactory (Test) portal link target. |

## Frontend container runtime (nginx)

The cockpit is served by nginx, whose config is templated with `envsubst` at
container start. These are container **runtime** env — not `VITE_*`, not backend
`Settings` — documented here for completeness.

| Variable | Default | Required? | Purpose |
|---|---|---|---|
| `BACKEND_URL` | `http://cfactory:80` | yes (in-cluster) | Upstream the nginx `/api` + `/health` + `/api/ws` proxy points at. |
| `CFACTORY_API_KEY` | _(empty)_ | when API keys are enforced | Bare CFactory key nginx injects as `Authorization: Bearer <key>` on `/api` + `/connect`, so the browser cockpit authenticates once the backend keystore is enforced. No `:scopes` suffix. |

## Secrets checklist

Set these to real values in any hosted/shared deployment — never commit them:

- `CFACTORY_AUDIT_HMAC_SECRET` — overrides the clearly-labelled dev default; startup hard-warns if the default is left in a non-local posture.
- `CFACTORY_UPSTREAM_TOKEN` / `CFACTORY_AIFACTORY_TOKEN` — upstream factory auth.
- `CFACTORY_API_KEYS` — scoped keys that gate the cockpit API.
- `CFACTORY_MCP_SECRET` — legacy full-scope credential for the MCP transport (`CFACTORY_API_KEYS` also gates `/mcp`, per declared tool scope).
- `CFACTORY_INTAKE_PROJECT_ID` — not a secret, but the one value a hosted deploy must decide deliberately: it is the repository autonomous card-driven builds land in.
- `CFACTORY_OLLAMA_API_KEY` / `ANTHROPIC_API_KEY` — copilot credentials.

## Notes

- The three upstream endpoints (`AIFACTORY`/`PFACTORY`/`TFACTORY_API_URL`) and
  the copilot provider + model are also editable at runtime from the cockpit
  (Services and Settings views). Runtime edits persist to small JSON files under
  the workspace root and survive a restart; the copilot API key is never written
  to disk.
- The RFC-0019 planning board reads three of these —
  `CFACTORY_INTAKE_PROJECT_ID`, `CFACTORY_API_KEYS` / `CFACTORY_MCP_SECRET`
  (board tool scopes) and `CFACTORY_MULTI_TENANT` (card partitioning). Each is
  written up with its user story, options and failure behaviour in
  [the planning-board guide](../guides/planning-board.md).
- Deep operator detail — per-variable read location, Helm knobs and chart gaps —
  lives in the in-repo TechDocs at `techdocs/dependencies.md`.

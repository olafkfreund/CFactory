# Dependencies

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.13 |
| Web | FastAPI (REST + WebSocket), `:3111` |
| ASGI server | uvicorn (h11 + wsproto for the `/api/ws` upgrade) |
| Agent runtime | Claude Agent SDK |
| Store | PostgreSQL via SQLAlchemy 2 + Alembic |
| Frontend | React 19 + TypeScript + Vite, `:3110` |
| Packaging | uv (Python), npm (frontend) |
| Containers | Docker (image + Helm chart under `charts/`) |
| Dev env | Nix flake + direnv; `just` task runner |

## Key Python dependencies

| Package | Role |
|---|---|
| `fastapi` / `uvicorn[standard]` | REST + WebSocket cockpit API |
| `claude-agent-sdk` | LLM copilot client (behind a test seam) |
| `httpx` | Synchronous per-service adapters + confirmed-action execution |
| `websockets` | Upstream live-feed subscribers |
| `wsproto` | WebSocket protocol for the cockpit `/api/ws` upgrade |
| `pydantic` ≥ 2 / `pydantic-settings` | Data models + `CFACTORY_*` settings |
| `sqlalchemy` ≥ 2 / `alembic` / `psycopg[binary]` | WorkItem + audit persistence |

## Key frontend dependencies

| Package | Role |
|---|---|
| `react` / `react-dom` 19 | Cockpit UI |
| `framer-motion` | Mission-control board animations |
| `vite` 6 / `typescript` 5 | Build + dev server (proxies `/api` to `:3111`) |

## Cross-service dependencies (Factory family)

CFactory **reads** the other three Factories' state and completion events — it sits
*downstream* of all of them in the observation graph and `dependsOn` each in the
Backstage catalog:

- **PFactory** (Plan) — adapter polls its REST plan surface; CFactory can propose
  `approve_gate` against its human-review gate.
- **AIFactory** (Act) — adapter polls its task surface; CFactory can propose
  `trigger_handoff` (create-and-run) and `kick_handback` (apply-correction).
- **TFactory** (Verify) — adapter polls its task/report surface for test verdicts.

How it reads each service:

- **REST** — on-demand hydration via per-service `BaseHTTPAdapter` (`/api/refresh`).
- **WebSocket** — opt-in live subscriptions to each `/api/ws` feed
  (`CFACTORY_SUBSCRIBE_UPSTREAMS`).
- **Webhook** — the RFC-0001 completion envelope POSTed to `/api/events` on terminal
  status.

**Contract:** [RFC-0001](https://factory.freundcloud.com/rfc/correlation-key/) — the
shared correlation key (GitHub issue number) + completion-event envelope, with the
v1.1 additive `usage` (token/cost) block that CFactory aggregates.

## Configuration (environment reference)

This is the complete reference for every environment variable CFactory reads —
backend (`CFACTORY_*`, plus a couple of bare-name aliases) and frontend
build-time (`VITE_*`). A fresh operator needs nothing beyond this table plus the
[`.env.example`](https://github.com/dataseeek/cfactory/blob/main/.env.example) at
the repo root. Every backend variable maps 1:1 to a field on the `Settings` class
in `apps/backend/cfactory/config.py` (`env_prefix="CFACTORY_"`), except
`CFACTORY_STALL_DEADLINE_SECONDS`, which is read directly in `store.py`.

For booleans, "on" is `true`/`1` and "off" is `false`/`0` (pydantic parsing);
the table gives the effect of each state.

### Backend (`CFACTORY_*`)

| Variable | Default | Required? | Purpose (on/off for booleans) | Read in | How to set |
|---|---|---|---|---|---|
| `CFACTORY_BACKEND_PORT` | `3111` | no | API server port. | `config.py` `backend_port` | env / Helm `config.backendPort` (ConfigMap) |
| `CFACTORY_FRONTEND_PORT` | `3110` | no | Cockpit UI port / CORS origin. | `config.py` `frontend_port` | env |
| `CFACTORY_WORKSPACE_ROOT` | `~/.cfactory` | no | Local state root (endpoint/copilot override JSON, SQLite). | `config.py` `workspace_root` | env / Helm volume mount `/home/nonroot/.cfactory` |
| `CFACTORY_AIFACTORY_API_URL` | `http://localhost:3101` | no | AIFactory (Act) upstream REST base. | `config.py` `aifactory_api_url` | env / Helm `config.aifactoryApiUrl` |
| `CFACTORY_PFACTORY_API_URL` | `http://localhost:3105` | no | PFactory (Plan) upstream REST base. | `config.py` `pfactory_api_url` | env / Helm `config.pfactoryApiUrl` |
| `CFACTORY_TFACTORY_API_URL` | `http://localhost:3103` | no | TFactory (Verify) upstream REST base. | `config.py` `tfactory_api_url` | env / Helm `config.tfactoryApiUrl` |
| `CFACTORY_OBSERVE_API_URL` | `http://localhost:5080` | no | OpenObserve health-probe base for the Services reachability view (not a PARR factory; never polled/hydrated). In-cluster: `http://observe.factory.svc.cluster.local:5080`. | `config.py` `observe_api_url` | env only — **no Helm knob** (add via `config.extraEnv`) |
| `CFACTORY_UPSTREAM_TOKEN` | `None` | in hosted | Shared bearer token sent as `Authorization: Bearer <token>` on every adapter call, poll and upstream WS. Leave unset only for local dev where the factories run `APP_DISABLE_AUTH=true`. Server-side only. | `config.py` `upstream_token` | env / Helm `upstreamToken` (Secret) |
| `CFACTORY_AIFACTORY_TOKEN` | `None` (falls back to `CFACTORY_UPSTREAM_TOKEN`) | no | Service token for AIFactory's live-agent console WebSocket (#34). Server-side only. | `config.py` `aifactory_token` | env only — **no Helm knob** (add via `config.extraEnv`) |
| `CFACTORY_DATABASE_URL` | `None` | no | Postgres connection string for the WorkItem correlation store. Unset = local SQLite under the workspace root. | `config.py` `database_url` | env / Helm `database` (Secret) |
| `CFACTORY_SUBSCRIBE_UPSTREAMS` | `false` | no | On: connect to each upstream `/api/ws` feed at startup. Off: no upstream WS (avoids reconnect loops against down services). | `config.py` `subscribe_upstreams` | env / Helm `config.subscribeUpstreams` |
| `CFACTORY_LIVE_PROGRESS` | `false` | no | On: poll PFactory/TFactory + subscribe AIFactory progress and broadcast `{type:"progress"}`. Off: no live-progress. | `config.py` `live_progress` | env |
| `CFACTORY_POLL_INTERVAL_SECONDS` | `3.0` | no | Live-progress poll interval, seconds. Only relevant when `CFACTORY_LIVE_PROGRESS` is on. | `config.py` `poll_interval_seconds` | env |
| `CFACTORY_STALL_DEADLINE_SECONDS` | `900.0` | no | Idle budget (seconds) before an active, non-terminal stage is flagged stalled (#105). Values `<= 0` or unparseable fall back to the default. | `store.py` `stall_deadline_seconds()` | env only — **no Helm knob** (add via `config.extraEnv`) |
| `CFACTORY_COPILOT_MODEL` | `claude-opus-4-8` | no | Copilot model id; meaning depends on `CFACTORY_COPILOT_PROVIDER`. Editable at runtime from the Settings view. | `config.py` `copilot_model` | env / Helm `config.copilotModel` |
| `CFACTORY_COPILOT_PROVIDER` | `claude` | no | Copilot LLM provider: `claude` (Claude Agent SDK, reads `ANTHROPIC_API_KEY`) or `ollama`/`openai_compatible` (OpenAI-compatible chat endpoint). | `config.py` `copilot_provider` | env / Helm `config.copilotProvider` |
| `CFACTORY_OLLAMA_CLOUD_BASE_URL` | `https://ollama.com/v1` | no | OpenAI-compatible copilot base URL (includes `/v1`). Also accepts the bare `OLLAMA_CLOUD_BASE_URL`. Only used when provider is not `claude`. | `config.py` `ollama_cloud_base_url` | env / Helm `config.ollamaCloudBaseUrl` |
| `CFACTORY_OLLAMA_API_KEY` | `None` | if provider is ollama | Bearer key for the OpenAI-compatible copilot endpoint. Also accepts the bare `OLLAMA_API_KEY` (shared factory secret). Server-side only. | `config.py` `ollama_api_key` | env / Helm `ollamaApiKey` (Secret) |
| `CFACTORY_API_KEYS` | `None` | in hosted | Scoped API keys `<key>:read,write;<key2>:read`. Unset = auth OPEN (single-user local mode); set = requests must carry a known key with the required scope. | `config.py` `api_keys` | env / Helm `apiKeys` (Secret) |
| `CFACTORY_MCP_SECRET` | `None` | in hosted | LEGACY full-scope bearer for the MCP transport (`POST /mcp`) (#113) — a caller presenting it holds `read` and `write`. Still the supported prod credential; scoped `CFACTORY_API_KEYS` work alongside it (#302). | `config.py` `mcp_secret` | env only — **no Helm knob** (add via `config.extraEnv`) |
| `CFACTORY_MCP_DEV_OPEN` | `false` | no | Explicit local-dev opt-in reopening `/mcp` when NO credential is configured; unconfigured otherwise DENIES (#302). Never set in a hosted deploy. | `config.py` `mcp_dev_open` | env only — **no Helm knob** (add via `config.extraEnv`) |
| `CFACTORY_PUBLIC_API_URL` | `None` | no | Public base URL of the token-gated API shown on `/settings/token` for editor/external clients. Display only. | `config.py` `public_api_url` | env / Helm `config.publicApiUrl` |
| `CFACTORY_MULTI_TENANT` | `false` | no | On: resolve tenant per request from the `X-Tenant-Id` header (hosted). Off: single `default` tenant. Per-tenant data scoping still deferred. | `config.py` `multi_tenant` | env / Helm `config.multiTenant` |
| `CFACTORY_AUDIT_HMAC_SECRET` | dev secret (`dev-insecure-...`) | in hosted | HMAC secret anchoring the tamper-evident audit chain (#21). MUST be overridden in any hosted/shared deploy (API keys or multi-tenant set) — the default is a clearly-labelled dev value and startup hard-warns if left in place. | `config.py` `audit_hmac_secret` | env / Helm `config.extraEnv` (Secret) |

External secret (read by the Claude Agent SDK, not by CFactory code, when
`CFACTORY_COPILOT_PROVIDER=claude`):

| Variable | Default | Required? | Purpose | Read in | How to set |
|---|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | `None` | if copilot provider is claude | Claude Agent SDK credential for the copilot. | Claude Agent SDK (via `copilot/service.py`) | env / Helm `config.extraEnv` (Secret) |

### Frontend (`VITE_*`, build-time)

`VITE_*` variables are read at **build time** by Vite and inlined into the static
bundle (`import.meta.env`) — they are not runtime container env. Set them as build
args / build-environment when running `npm run build` (or in the frontend image
build). Each has a hard-coded fallback, so all are optional.

| Variable | Default (fallback) | Required? | Purpose | Read in | How to set |
|---|---|---|---|---|---|
| `VITE_OBSERVE_URL` | `undefined` (feature hidden) | no | OpenObserve dashboard URL linked from the Services view / header. Unset = the observe link is not shown. | `src/dashboard.ts`, `src/ServicesView.tsx` | frontend build arg |
| `VITE_PFACTORY_URL` | `https://pfactory.freundcloud.org.uk` | no | PFactory (Plan) portal link target in the cockpit stage rail. | `src/dashboard.ts` | frontend build arg |
| `VITE_AIFACTORY_URL` | `https://aifactory.freundcloud.org.uk` | no | AIFactory (Build) portal link target. | `src/dashboard.ts` | frontend build arg |
| `VITE_TFACTORY_URL` | `https://tfactory.freundcloud.org.uk` | no | TFactory (Test) portal link target. | `src/dashboard.ts` | frontend build arg |

### Frontend container runtime (nginx, not build-time)

The cockpit is served by nginx, whose config is templated with `envsubst` at
container start. These are container **runtime** env (not `VITE_*`, not backend
`Settings`), documented here for completeness:

| Variable | Default | Required? | Purpose | Set via |
|---|---|---|---|---|
| `BACKEND_URL` | `http://<release>:<port>` | yes (in-cluster) | Upstream the nginx `/api` + `/health` + `/api/ws` proxy points at. | Helm `frontend-deployment.yaml` (auto) |
| `CFACTORY_API_KEY` | (unset) | when `apiKeys.enabled` | Bare CFactory key nginx injects as `Authorization: Bearer <key>` on `/api` + `/connect`, so the browser cockpit authenticates once the backend keystore is enforced. No `:scopes` suffix. | Helm `frontend.apiKey` (Secret) |

## Helm chart gaps

These variables are read by the app but have **no dedicated `values.yaml` knob**
today; set them through `config.extraEnv` (backend) until a knob is added:

- `CFACTORY_OBSERVE_API_URL` — the Services reachability view falls back to the
  local dev default in-cluster unless set.
- `CFACTORY_AIFACTORY_TOKEN` — live-agent console WS token; without it the console
  falls back to `CFACTORY_UPSTREAM_TOKEN`.
- `CFACTORY_MCP_SECRET` / `CFACTORY_API_KEYS` — the MCP transport DENIES every request until one of them is set (#302).
- `CFACTORY_STALL_DEADLINE_SECONDS`, `CFACTORY_POLL_INTERVAL_SECONDS`,
  `CFACTORY_LIVE_PROGRESS`, `CFACTORY_FRONTEND_PORT`, `ANTHROPIC_API_KEY` — no
  first-class knob; use `config.extraEnv`.

## Secrets

Set these to real values in any hosted/shared deployment (never commit them):

- `CFACTORY_AUDIT_HMAC_SECRET` — overrides the clearly-labelled dev default;
  startup hard-warns if the default is left in a non-local posture.
- `CFACTORY_UPSTREAM_TOKEN` / `CFACTORY_AIFACTORY_TOKEN` — upstream factory auth.
- `CFACTORY_API_KEYS` — scoped keys that gate the cockpit API.
- `CFACTORY_MCP_SECRET` — legacy full-scope credential for the MCP transport
  (`CFACTORY_API_KEYS` also gates `/mcp`, per declared tool scope).
- `CFACTORY_OLLAMA_API_KEY` / `ANTHROPIC_API_KEY` — copilot credentials.

## Completeness

Every environment variable read anywhere in the codebase is accounted for above.

**Backend** — 23 `CFACTORY_*` names (22 `Settings` fields + `CFACTORY_STALL_DEADLINE_SECONDS`
in `store.py`), 2 bare-name aliases (`OLLAMA_CLOUD_BASE_URL`, `OLLAMA_API_KEY`),
1 external SDK secret (`ANTHROPIC_API_KEY`), 2 frontend-container runtime env
(`BACKEND_URL`, `CFACTORY_API_KEY`). Found = documented; 0 unaccounted.

**Frontend** — 4 `VITE_*` names (`VITE_OBSERVE_URL`, `VITE_PFACTORY_URL`,
`VITE_AIFACTORY_URL`, `VITE_TFACTORY_URL`). Found = documented; 0 unaccounted.

### Intentionally excluded (incidental)

- `MYPYPATH` (`scripts/ratchet_lint.py`) — set by the lint/type-check dev tooling,
  not read by the running service.
- Vite/vitest internal `import.meta.env` machinery under `node_modules/` — library
  code, not application configuration.

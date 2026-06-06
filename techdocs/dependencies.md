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

## Configuration (`CFACTORY_*`)

| Setting | Default | Purpose |
|---|---|---|
| `CFACTORY_BACKEND_PORT` | `3111` | API server port |
| `CFACTORY_FRONTEND_PORT` | `3110` | Cockpit UI / CORS origin |
| `CFACTORY_WORKSPACE_ROOT` | `~/.cfactory` | Local state root |
| `CFACTORY_AIFACTORY_API_URL` | `http://localhost:3101` | AIFactory upstream |
| `CFACTORY_PFACTORY_API_URL` | `http://localhost:3105` | PFactory upstream |
| `CFACTORY_TFACTORY_API_URL` | `http://localhost:3103` | TFactory upstream |
| `CFACTORY_DATABASE_URL` | `None` | Postgres (else local SQLite) |
| `CFACTORY_SUBSCRIBE_UPSTREAMS` | `false` | Connect to upstream WS feeds on startup |
| `CFACTORY_LIVE_PROGRESS` | `false` | Poll/subscribe live progress |
| `CFACTORY_COPILOT_MODEL` | `claude-opus-4-8` | Copilot model (SDK reads `ANTHROPIC_API_KEY`) |
| `CFACTORY_API_KEYS` | `None` | Scoped keys `<key>:read,write;...`; OPEN when unset |
| `CFACTORY_MULTI_TENANT` | `false` | Resolve tenant from `X-Tenant-Id` (hosted) |
| `CFACTORY_AUDIT_HMAC_SECRET` | dev secret | Anchors the tamper-evident audit chain |

## Secrets

The copilot reads `ANTHROPIC_API_KEY` from the environment. `CFACTORY_AUDIT_HMAC_SECRET`
must be set to a real secret in any hosted/shared deployment — the bundled default is a
clearly-labelled dev value.

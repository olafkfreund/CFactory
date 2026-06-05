# Architecture

CFactory is the **control tower** of the Factory PARR pipeline. Its single job is to
**observe and correlate** the other three services and present them as one board with
an advise-and-confirm copilot. It deliberately owns no pipeline logic of its own.

## Pure-consumer design

```
   PFactory :3102        AIFactory :3101        TFactory :3103
        │  REST/WS             │  REST/WS             │  REST/WS
        │  + completion        │  + completion        │  + completion
        │    webhook           │    webhook           │    webhook
        ▼                      ▼                      ▼
   ┌──────────────────────────────────────────────────────────┐
   │                       CFactory  :3111                     │
   │                                                          │
   │   adapters ──▶  POST /api/events  ──▶  WorkItem store    │
   │   (poll)         (RFC-0001 ingress)     (correlation)     │
   │                          │                                │
   │                          ▼                                │
   │          copilot (Claude Agent SDK)  ──▶  /api/ws  ──────────▶ Cockpit :3110
   │          advise-and-confirm actions                       │
   └──────────────────────────────────────────────────────────┘
```

Integration is **loose** — over each service's existing API plus an opt-in
completion webhook — never a shared database. CFactory reads through the APIs that
already exist and writes only through human-confirmed actions.

## Components

The FastAPI app object is `app = create_app()` in `apps/backend/cfactory/app.py`
(title *CFactory*, version from `cfactory.__version__`). All routes are declared
inline in `create_app()` — there is no separate router module.

### 1. Ingress & adapters

CFactory hydrates its store two ways:

- **Push** — the [completion-event ingress](apis/events.md) at `POST /api/events`
  (alias `POST /api/events/completion`). Upstream services POST the RFC-0001 envelope
  on terminal status; the event is upserted onto the WorkItem timeline.
- **Pull** — adapters in `apps/backend/cfactory/adapters/` poll each upstream over
  `httpx`: `PFactoryAdapter` (`/api/plans`), `AIFactoryAdapter` (`/api/tasks`),
  `TFactoryAdapter` (`/api/tasks`), all built on `BaseHTTPAdapter`. `POST /api/refresh`
  triggers a full poll-and-hydrate and broadcasts a `snapshot`.

Two **opt-in** live integrations exist (off by default so dev/tests don't reconnect-loop
against down services):

- an upstream **WebSocket subscriber** (`upstream_ws.py`, gated by `CFACTORY_SUBSCRIBE_UPSTREAMS`),
- a **live-progress poller** (`progress.py`, gated by `CFACTORY_LIVE_PROGRESS`).

### 2. The WorkItem correlation store

The heart of CFactory. Model in `apps/backend/cfactory/models.py`, persistence in
`store.py`.

- A **`WorkItem`** is keyed by **`correlation_key` = the GitHub issue number**
  (synthetic fallback = the service `task_id` when no issue exists yet).
- It aggregates three **`ServiceState`** slices — `pfactory` (plan), `aifactory`
  (code), `tfactory` (test) — each holding `task_id`, `status`, `phase`, optional
  `usage` (`TokenUsage`) and `extra`.
- It carries a **`timeline: list[CompletionEvent]`** appended on each applied event.
- Persistence is SQLAlchemy ORM `WorkItemRow` (table `work_items`, unique-indexed
  `correlation_key`; slices + timeline stored as JSON columns).
  `upsert_from_event` is **idempotent** by `(service, correlation_key, status)`;
  `upsert_snapshot` reflects polled adapter state without adding a timeline entry.

This is the cross-service identity the family otherwise lacks: one row gives the full
plan → code → test story of a unit of work, plus its token spend.

### 3. The copilot (advise-and-confirm)

`apps/backend/cfactory/copilot/`. The copilot runs on the **Claude Agent SDK**
(`ClaudeSDKClient` / `ClaudeAgentOptions`, model from `CFACTORY_COPILOT_MODEL`,
default `claude-sonnet-4-6`). The LLM call sits behind an injectable `AgentRunner`
seam so tests stay hermetic.

- **Read tools** (`copilot/tools.py`): `query_work_items`, `summarize_timeline`,
  `rollups`, `token_totals`; anomaly detection (`copilot/anomalies.py`) flags
  `failure`, `handback_loop` and `stuck` work items.
- `POST /api/copilot/ask` answers a question against a compact board snapshot +
  rollups + anomaly summary (synchronous JSON, not streaming).

The **advise-and-confirm gate** lives in `actions.py`: `propose_*` only **builds** a
`PreparedAction` (it never touches an upstream). The single write path is the separate
`POST /api/actions/execute` → `execute_action()`, which requires the `write` scope and
records an audit entry. The frontend mirrors this — a "Propose" button calls
`proposeAction`; a distinct "Confirm" button calls `executeAction`. The model never
invokes a write autonomously. `is_safe_endpoint` adds SSRF defense on action targets.

### 4. The cockpit (frontend)

`apps/frontend-web/` — a React 19 + Vite 6 single-page app with six views:
**Mission Control**, the **Pipeline** board (three columns Plan/Code/Test with live
badges, event counts and relative times), **Tokens**, **Copilot** (insights +
anomaly cards with the Propose→Confirm flow + a chat thread), **Audit**, and
**Services** (upstream health). `src/api.ts` does REST fetches plus an `openFeed`
WebSocket to `/api/ws` for live `snapshot`/`progress`/`workitem` updates.

## Lifecycle of a completion event

1. An upstream service finishes a stage and POSTs the RFC-0001 envelope to
   `POST /api/events`.
2. `ingest_event` validates it against the `CompletionEvent` Pydantic model and calls
   `store.upsert_from_event` — idempotent by `(service, correlation_key, status)`.
3. The matching `WorkItem` slice is updated, the event appended to its timeline, and
   any `usage` block folded into the token rollups.
4. A `workitem` message is broadcast on `/api/ws`; every connected cockpit updates live.
5. If the copilot's anomaly pass later flags the item, it surfaces an advice card — and
   any remediation stays gated behind an explicit human **Confirm**.

## Ports, storage & config

Config is `apps/backend/cfactory/config.py` (env prefix `CFACTORY_`, reads `.env`):

| Setting | Default |
|---|---|
| Backend / API port | **3111** |
| Frontend / cockpit port | **3110** |
| Workspace root | `~/.cfactory` |
| Database | none → SQLite `~/.cfactory/cfactory.db`; prod via `CFACTORY_DATABASE_URL` (PostgreSQL) |
| Upstreams | AIFactory `:3101` · PFactory `:3102` · TFactory `:3103` |

The Nix dev shell (`flake.nix`, `cfactory-dev`: Python 3.13, Node 22, `uv`, Postgres
client) exports these, and the `justfile` wires `run` (uvicorn on 3111) + `ui` (Vite on
3110, proxying `/api` and `/health` to 3111). The `Dockerfile` runs `cfactory.app:app`
non-root on 3111 with `--http h11 --ws wsproto` (httptools doesn't forward the WS
Upgrade).

## Security model

API keys live in `auth.py`. **Local-first**: when `CFACTORY_API_KEYS` is empty the
keystore is *open* (every request allowed). When configured, requests carry a key via
`Authorization: Bearer` or `X-API-Key` with `read`/`write` scopes
(`"<key>:read,write;<key2>:read"`). Only `POST /api/actions/execute` enforces a scope
(`write`). Every confirmed action is recorded in a **tamper-evident HMAC-anchored audit
chain** (`audit.py`, migration `557faa62dcdc_audit_hmac_chain.py`). Enterprise SAML/SCIM
and multi-tenancy are documented seams (`enterprise.py`) deferred to the hosted
deployment.

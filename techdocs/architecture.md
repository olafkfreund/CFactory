# Architecture

CFactory is a **read-first, advise-and-confirm cockpit** over the three Factory
services. It observes, correlates and (with a human click) steers them through their
existing APIs — it runs no pipeline logic of its own.

## The big picture

```
        ┌───────────┐     ┌───────────┐     ┌───────────┐
        │ PFactory  │     │ AIFactory │     │ TFactory  │
        │  :3105    │     │  :3101    │     │  :3103    │
        │  (Plan)   │ ──▶ │  (Act)    │ ──▶ │ (Verify)  │
        └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
              │  REST · WebSocket · completion webhooks
              ▼                 ▼                 ▼
        ┌──────────────────────────────────────────────┐
        │                  CFactory                     │
        │   adapters ─▶ WorkItem store ─▶ copilot       │
        │   webhook ingress     cockpit UI :3110        │
        │              backend API :3111                │
        └──────────────────────────────────────────────┘
```

The canonical local upstream port map (UI / API) is **AIFactory 3100/3101 ·
TFactory 3102/3103 · PFactory 3104/3105** (`apps/backend/cfactory/config.py`);
endpoints are editable at runtime from the Services view. CFactory itself runs the
cockpit UI on **3110** and the backend API on **3111**.

## Components

```
apps/
├── backend/
│   └── cfactory/
│       ├── app.py            # FastAPI app factory — all routes + the /api/ws hub
│       ├── config.py         # CFACTORY_* settings (ports, upstream URLs, flags)
│       ├── models.py         # WorkItem · CompletionEvent · ServiceState · TokenUsage
│       ├── store.py          # WorkItem correlation store (SQLAlchemy / Postgres)
│       ├── adapters/         # per-service REST clients → normalized AdapterItem
│       │   ├── base.py           # BaseHTTPAdapter + AdapterItem + dotted-key `first`
│       │   ├── pfactory.py / aifactory.py / tfactory.py
│       ├── upstream_ws.py    # live WebSocket subscribers (reconnect/backoff)
│       ├── ws.py             # ConnectionManager — broadcast hub to cockpits
│       ├── progress.py       # live in-flight progress hub
│       ├── copilot/          # agentic copilot (Claude Agent SDK)
│       │   ├── service.py        # Copilot + runner seam + board snapshot
│       │   ├── tools.py          # read tools: rollups, token totals, timelines
│       │   └── anomalies.py      # stuck / handback-loop / failure detection
│       ├── actions.py        # PreparedAction: propose → confirm → execute (SSRF-guarded)
│       ├── audit.py          # HMAC-anchored, tamper-evident action audit chain
│       ├── auth.py           # scoped API keys (read/write); OPEN in local mode
│       ├── enterprise.py     # identity + multi-tenant resolution seams (deferred)
│       ├── db.py             # SQLAlchemy Base + engine factory
│       └── migrations/       # Alembic migrations (work items, audit, HMAC chain)
└── frontend-web/            # React 19 + Vite cockpit UI (:3110)
    └── src/                  # MissionControl · CopilotPanel · AuditView · TokensView
```

## The data plane

CFactory deliberately uses each service's **existing** surface rather than its stdio
MCP server (which is spawned per-process by an LLM client and unsuited to a persistent
dashboard):

- **REST (pull / state)** — one `BaseHTTPAdapter` per service hydrates the store on
  demand via `POST /api/refresh`; schema drift is absorbed by a dotted-key `first()`
  helper.
- **WebSocket (live)** — `upstream_ws.py` subscribes to each service's `/api/ws` feed
  (opt-in: `CFACTORY_SUBSCRIBE_UPSTREAMS`), parses messages into `CompletionEvent`s,
  and rebroadcasts to connected cockpits via `ws.ConnectionManager`.
- **Webhooks (terminal)** — `POST /api/events` (alias `/api/events/completion`)
  ingests the RFC-0001 normalized completion envelope and upserts the WorkItem
  timeline. Idempotent by `(service, correlation_key, status)`.

## The linchpin: the WorkItem

The family lacked a shared identity for a unit of work. CFactory introduces it. A
`WorkItem` is keyed by the **GitHub issue number** (synthetic fallback otherwise) and
threads:

```
pfactory.session_id → github issue # → aifactory.task_id → branch / PR # → tfactory.spec_id
```

It carries a per-service `ServiceState` slice (status · phase · optional `TokenUsage`)
plus an ordered `timeline` of completion events — so the cockpit and copilot answer
"where is feature X" with history, not just a live snapshot.

## The agentic copilot

A Claude Agent SDK layer whose tools are CFactory's *own* functions:

- **Read tools** — query WorkItems, summarise a timeline, compute rollups and token
  totals, detect anomalies. The LLM call is isolated behind a `runner` seam so the
  test suite needs neither the SDK nor `ANTHROPIC_API_KEY`.
- **Action tools (advise + confirm)** — `propose_approve_gate`,
  `propose_trigger_handoff`, `propose_kick_handback`. Each builds a `PreparedAction`
  (target service · endpoint · payload · rationale) that only executes on an explicit
  `POST /api/actions/execute`. **No autonomous writes.**

## Safety & governance

- **SSRF guard** — `PreparedAction.endpoint` must be a root-relative path; absolute or
  protocol-relative URLs are rejected at the model validator *and* re-checked in
  `execute_action`, so a confirmed action can only ever hit the resolved per-service
  base URL.
- **Audit chain** — every confirmed action is recorded with an HMAC-SHA256 hash
  chained to the prior entry (`audit.py`), making after-the-fact mutation, reordering
  or deletion detectable (`AuditStore.verify`).
  - Appending is serialised: the tail read and the insert are one critical section
    (`BEGIN IMMEDIATE` on SQLite, a transaction advisory lock on PostgreSQL).
    Without it, two concurrent confirms both chain to the same predecessor and the
    chain forks — which is what happened live on 2026-07-30 (#306).
  - `AuditStore.check()` classifies what it finds: `mutated` (a field or hash was
    edited), `duplicate` (a row was replayed), `dangling` (a row was deleted or
    reordered), and `forked` (two valid entries share a predecessor — the write
    race above). `AuditStore.verify()` is the alarm and reports the first three;
    a fork is reported by `check()` only, so a race this store used to permit
    cannot mask a real tamper by keeping the alarm permanently on. Read the chain
    on a running deployment with
    `AuditStore(url, create=False).check()`.
- **Scoped keys** — `auth.py` enforces `read`/`write` scopes when `CFACTORY_API_KEYS`
  is set; local single-user mode is OPEN by default.
- **Multi-tenant** — `enterprise.py` ships the identity + tenant-resolution seams;
  per-tenant data scoping and SAML/SCIM are deferred to the hosted deployment.

## Runtime

- **Backend:** `python apps/backend/run.py` → uvicorn on `:3111` (h11 + wsproto so the
  `/api/ws` upgrade works). `just run` wraps it.
- **Cockpit UI:** `cd apps/frontend-web && npm run dev` → Vite on `:3110`, proxying
  `/api` (incl. `ws`) and `/health` to the backend. `just ui` wraps it.
- **Store:** PostgreSQL via SQLAlchemy + Alembic (`just db-upgrade`).

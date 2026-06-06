# API & WebSocket

CFactory exposes two programmable surfaces:

1. **REST API** — the FastAPI cockpit backend (`apps/backend/cfactory/app.py`).
2. **WebSocket** — a single live broadcast feed for connected cockpits.

Unlike the upstream Factory services, CFactory does **not** ship its own MCP server or
CLI — it *consumes* the other services' REST/WebSocket surfaces (see
[DEC-002](decisions.md)). The only runtime entrypoint is `apps/backend/run.py`.

## REST API

- **Base URL:** `http://localhost:3111` (default `CFACTORY_BACKEND_PORT`); hosted at
  `https://cfactory.freundcloud.com`.
- **Auth:** local single-user mode is **OPEN**. When `CFACTORY_API_KEYS` is configured,
  write endpoints require a key with the `write` scope, sent as
  `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- **Live contract (source of truth):** `GET /openapi.json`. The
  [curated OpenAPI spec](https://github.com/olafkfreund/CFactory/blob/main/openapi.yaml)
  registered in the Backstage catalog is a hand-maintained subset.

### Event ingestion

| Method & path | Purpose |
|---|---|
| `POST /api/events` | Ingest an RFC-0001 completion event (idempotent) |
| `POST /api/events/completion` | RFC-documented alias for the above |
| `POST /api/refresh` | Poll every upstream service and hydrate the store (best-effort) |

### WorkItem board (read)

| Method & path | Purpose |
|---|---|
| `GET /api/workitems` | List all correlation-keyed work items |
| `GET /api/workitems/{correlation_key}` | Get one work item |
| `GET /api/workitems/{correlation_key}/timeline` | Ordered event timeline + total span |
| `GET /api/workitems/{correlation_key}/process` | Live process detail (phase, progress %, subtasks) — proxied from the code task. See [Task detail](task-detail.md) |

### Insights (read)

| Method & path | Purpose |
|---|---|
| `GET /api/rollups` | Counts + latency across the board (cost when instrumented) |
| `GET /api/tokens` | Token/cost totals by service + work item (RFC-0001 `usage`) |
| `GET /api/progress` | Live in-flight progress snapshot |
| `GET /api/anomalies` | Stuck phases, handback loops, gate/test failures |

### Live agents (read-only)

| Method & path | Purpose |
|---|---|
| `GET /api/live-agents` | Active AIFactory agent sessions to stream (capability-gated) |
| `WS /api/live-agents/{correlation_key}/ws` | Proxy of an agent's rmux console — ANSI pane bytes for xterm.js |

See [Live agents](live-agents.md) for the data path, the read-only guarantee, and
the `CFACTORY_AIFACTORY_TOKEN` / `AIFACTORY_RMUX_ENABLED` wiring.

### Copilot

| Method & path | Purpose |
|---|---|
| `POST /api/copilot/ask` | Ask the read-only pipeline copilot over the board snapshot |

The copilot reasons via the Claude Agent SDK behind a runner seam; it never writes to
an upstream service. Model is set by `CFACTORY_COPILOT_MODEL` (default
`claude-opus-4-8`); the SDK reads `ANTHROPIC_API_KEY` from the environment.

### Actions — propose → confirm → execute

| Method & path | Purpose |
|---|---|
| `POST /api/actions/propose` | Build (but do NOT execute) a `PreparedAction` — advise only |
| `POST /api/actions/execute` | Run a CONFIRMED `PreparedAction` (requires `write` scope) |
| `GET /api/audit` | Recent confirmed actions, newest first — the HITL trail |

Action kinds:

| Kind | Target | Endpoint (best-effort contract) |
|---|---|---|
| `approve_gate` | PFactory | `POST /api/plans/{session}/approve` |
| `trigger_handoff` | AIFactory | `POST /api/tasks/create-and-run` |
| `kick_handback` | AIFactory | `POST /api/tasks/{task_id}/apply-correction` |

!!! note "SSRF guard"
    `PreparedAction.endpoint` must be a **root-relative path** (no scheme, no host).
    It is validated at the Pydantic model *and* re-checked in `execute_action`, so a
    confirmed action can only ever reach the resolved per-service base URL.

Every confirmed action is recorded in the HMAC-anchored audit chain before its result
is returned (see [DEC-005](decisions.md)).

### Health

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness + the resolved upstream URLs + multi-tenant flag |

## WebSocket

A single cockpit feed at **`/api/ws`** (`apps/backend/cfactory/ws.py`). The server
pushes JSON frames to every connected cockpit; clients are not expected to send
messages.

| Frame `type` | Payload |
|---|---|
| `workitem` | A single upserted `WorkItem` (from an event/upstream message) |
| `snapshot` | The full board, after `POST /api/refresh` |
| `progress` | Live in-flight progress (when `CFACTORY_LIVE_PROGRESS` is on) |

CFactory also **subscribes** to each upstream service's own `/api/ws` feed when
`CFACTORY_SUBSCRIBE_UPSTREAMS=true`, parsing messages into `CompletionEvent`s and
rebroadcasting them on its own feed (`apps/backend/cfactory/upstream_ws.py`).

The Vite dev server (`:3110`) proxies `/api` (with `ws: true`) and `/health` to the
backend, so the cockpit talks to a single origin in development.

## Running

```bash
# Backend API on :3111 (h11 + wsproto so the /api/ws upgrade works)
just run            # or: PYTHONPATH=apps/backend apps/backend/.venv/bin/python apps/backend/run.py

# Cockpit UI on :3110 (proxies /api + /health to :3111)
just ui             # or: cd apps/frontend-web && npm run dev
```

# Web REST + WebSocket API

FastAPI app `cfactory.app:app` (v0.1.0), served on **port 3111**. The machine-readable
contract is the generated OpenAPI 3.1 document at
[`techdocs/specs/cfactory-web-api.openapi.json`](../specs/cfactory-web-api.openapi.json)
(Backstage renders it under the **API** tab of `cfactory-web-api`). This page is the
curated tour; the spec is the source of truth.

All routes are declared inline in `create_app()` (`apps/backend/cfactory/app.py`) — there
is no `APIRouter`/prefix object.

## Health & events

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Status, version, `multi_tenant` flag, upstream URLs. |
| `POST` | `/api/events` | RFC-0001 completion-event ingress. See [Completion-event ingress](events.md). |
| `POST` | `/api/events/completion` | Alias of `/api/events` (same handler). |
| `POST` | `/api/refresh` | Poll every upstream adapter, hydrate the store, broadcast a `snapshot`. |

`POST /api/events` returns `{status: accepted | duplicate, correlation_key}` and is
idempotent by `(service, correlation_key, status)`.

## WorkItem read API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workitems` | List all WorkItems. |
| `GET` | `/api/workitems/{correlation_key}` | Get one (404 if missing). |
| `GET` | `/api/workitems/{correlation_key}/timeline` | Ordered event timeline. |
| `GET` | `/api/rollups` | Aggregate roll-ups across WorkItems. |
| `GET` | `/api/tokens` | Token / cost totals (from RFC-0001 `usage`). |
| `GET` | `/api/progress` | Live progress snapshot. |
| `GET` | `/api/anomalies` | Detected anomalies (failure / handback loop / stuck). |

## Actions (advise-and-confirm)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/actions/propose` | Build a `PreparedAction` — **advice only**, never executes. |
| `POST` | `/api/actions/execute` | Run a **confirmed** action. Requires `write` scope; records an audit entry. The only write path. |
| `GET` | `/api/audit` | The human-in-the-loop audit trail, newest first. |

This two-step split is the heart of CFactory's control model — see
[Decisions](../decisions.md).

## Copilot

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/copilot/ask` | `{question}` → `{answer, work_items_considered}`. Synchronous JSON (runs `copilot.ask` in a threadpool). |

## WebSocket

| Path | Direction | Purpose |
|---|---|---|
| `WS /api/ws` | server → client | Cockpit broadcast feed. Pushes `workitem`, `snapshot` and `progress` messages for live updates. |

## Authentication

Scoped API keys (`apps/backend/cfactory/auth.py`):

- **Local-first** — when `CFACTORY_API_KEYS` is empty the keystore is *open* and every
  request is allowed (single-user/dev default).
- **Configured** — keys are supplied as `"<key>:read,write;<key2>:read"`. Requests carry
  one via `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- Only `POST /api/actions/execute` enforces a scope (`write`); read endpoints need only a
  valid key (or none, when open).

# Completion-event ingress

This is the contract that makes CFactory the family's observer. The machine-readable
definition is [`techdocs/specs/cfactory-events.asyncapi.yaml`](../specs/cfactory-events.asyncapi.yaml)
(AsyncAPI 2.6); the canonical, authoritative version of the envelope lives in the
[Factory RFC-0001](https://factory.freundcloud.com/rfc/correlation-key/) — defer to it
for the precise schema.

## How it arrives

Every upstream service emits one event when its stage reaches a terminal status and
POSTs it to CFactory:

```
POST /api/events            (or the alias POST /api/events/completion)
Content-Type: application/json
```

The handler is `ingest_event` in `apps/backend/cfactory/app.py`; the body is validated
against the `CompletionEvent` Pydantic model in `apps/backend/cfactory/models.py`.

## The envelope

| Field | Type | Required | Notes |
|---|---|:--:|---|
| `correlation_key` | string | ✅ | GitHub issue number as a string; synthetic `pf-/af-/tf-` fallback. Never null. |
| `service` | enum | ✅ | `pfactory` \| `aifactory` \| `tfactory`. Selects the WorkItem slice. |
| `task_id` | string | ✅ | The emitting service's own task/spec/session id. |
| `status` | string | ✅ | Terminal status string for the stage. |
| `phase` | string \| null | — | Coarse PARR phase (`plan` \| `act` \| `test`). |
| `updated_at` | date-time | ✅ | ISO-8601 UTC timestamp of the transition. |
| `usage` | object \| null | — | Additive RFC-0001 **v1.1** token/cost block (see below). |

### The additive `usage` block (v1.1)

Optional and additive — v1 emitters omit it, and CFactory tolerates its absence:

```jsonc
"usage": {
  "input_tokens": 18234,
  "output_tokens": 4096,
  "total_tokens": 22330,
  "cost_usd": 0.21,
  "model": "claude-sonnet-4-6"
}
```

CFactory folds `usage` into the WorkItem slice and the `/api/tokens` rollups so spend can
be aggregated by service and by work item.

## Example

```json
{
  "correlation_key": "142",
  "service": "tfactory",
  "task_id": "spec-001",
  "status": "triaged",
  "phase": "test",
  "updated_at": "2026-06-05T12:00:00Z"
}
```

Response:

```json
{ "status": "accepted", "correlation_key": "142" }
```

## How it's consumed

`store.upsert_from_event` (`apps/backend/cfactory/store.py`) is **idempotent** by the
tuple `(service, correlation_key, status)` (RFC-0001 §7), so a redelivered webhook is a
no-op (`{status: "duplicate"}`). On a fresh event the matching slice is updated, the
event is appended to the WorkItem timeline, and a `workitem` message is broadcast on
`/api/ws`. One ingestion path, no shared database.

!!! note "Where the upstream side lives"
    The emitters are owned by the upstream services: see the
    `aifactory-completion-event` and `tfactory-completion-event` API entities. CFactory
    only defines the *consumer* contract here.

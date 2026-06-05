# APIs

CFactory exposes a small surface and consumes a large one — fitting for an observer.

## Provided

| API | Kind | Definition | Summary |
|---|---|---|---|
| **[cfactory-web-api](rest-api.md)** | `openapi` | [`techdocs/specs/cfactory-web-api.openapi.json`](../specs/cfactory-web-api.openapi.json) | REST + WebSocket control plane (port 3111) — WorkItems, rollups, the events ingress, advise-and-confirm actions, audit, copilot. |
| **[cfactory-events-ingress](events.md)** | `asyncapi` | [`techdocs/specs/cfactory-events.asyncapi.yaml`](../specs/cfactory-events.asyncapi.yaml) | The RFC-0001 v1.1 completion-event envelope CFactory accepts at `POST /api/events`. |

The OpenAPI document is **generated from the FastAPI app** (`cfactory.app:app`,
OpenAPI 3.1.0, ~15 paths). Regenerate it with:

```bash
cd apps/backend
PYTHONPATH=. .venv/bin/python -c \
  "import json; from cfactory.app import app; \
   json.dump(app.openapi(), open('../../techdocs/specs/cfactory-web-api.openapi.json','w'), indent=2)"
```

WebSocket routes (`/api/ws`) are not part of the OpenAPI document — see
[Web REST + WebSocket API](rest-api.md).

## Consumed

CFactory is a pure consumer; in the Backstage catalog it `consumesApis`:

- `pfactory-api` — PFactory's REST surface (poll `/api/plans`).
- `aifactory-web-api` — AIFactory's REST surface (poll `/api/tasks`).
- `tfactory-web-api` — TFactory's REST surface (poll `/api/tasks`).
- `aifactory-completion-event`, `tfactory-completion-event` — the upstream
  completion events delivered to CFactory's ingress.

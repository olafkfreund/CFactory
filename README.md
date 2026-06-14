# CFactory
CFactory — the agentic control-tower cockpit over the PARR pipeline. One pane of glass + LLM copilot across PFactory, AIFactory and TFactory. Ports 3110/3111.

Recently shipped:

- **Live execution diagram** — an animated dependency-graph in the task-detail drawer that draws whichever PARR stage is furthest along (test, else code, else plan) from a shared `graph` field on `GET /api/workitems/{key}/process`. Done nodes turn green with a robot stamp, active nodes pulse cyan, failed shake red, stalled pulse amber; the edge the work is flowing along animates with marching dashes, and each node carries a live mm:ss timer.
- **Per-task cost and tokens** — a "Cost & tokens by task" panel in Mission Control plus a live cost/token stamp on running task cards, sourced from CFactory's own `/api/tokens` event store.
- **Per-worker observability** — per-worker / per-provider cost and token attribution, drilled down from `GET /api/tokens/by_worker`.

See the [architecture docs](docs/architecture.md) for details.

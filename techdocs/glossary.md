# Glossary

**Advise-and-confirm** — CFactory's control model: the copilot *proposes* an action
(`/api/actions/propose`), a human *confirms* it (`/api/actions/execute`). No autonomous
writes.

**Anomaly** — a condition the copilot flags on a WorkItem: `failure`, `handback_loop`
(test→fix ping-pong), or `stuck`. Surfaced as advice cards in the Copilot view.

**Completion event** — the normalized RFC-0001 envelope an upstream service POSTs to
`/api/events` on terminal status. See [Completion-event ingress](apis/events.md).

**Correlation key** — the shared identifier threading one unit of work across the family:
the **GitHub issue number** (synthetic `pf-/af-/tf-` fallback when no issue exists yet).

**Cockpit** — the React frontend (port 3110): the "one pane of glass" over the pipeline.

**Copilot** — CFactory's Claude Agent SDK assistant. Reads the board (WorkItems, rollups,
anomalies) to answer questions and propose remediations; never writes on its own.

**PreparedAction** — the object `propose` builds describing a write that *would* happen.
It is inert until `execute` runs it under a human confirm + `write` scope.

**Rollup** — an aggregate over WorkItems (counts by stage/status, token/cost totals)
exposed at `/api/rollups` and `/api/tokens`.

**ServiceState** — one of the three slices (`pfactory`, `aifactory`, `tfactory`) a WorkItem
aggregates: `task_id`, `status`, `phase`, optional `usage`, `extra`.

**Snapshot** — a full board state broadcast on `/api/ws` after `POST /api/refresh` polls
the upstream adapters.

**Timeline** — the ordered list of completion events recorded on a WorkItem.

**Upstreams** — the three observed services: PFactory (`:3102`), AIFactory (`:3101`),
TFactory (`:3103`).

**WorkItem** — CFactory's central entity: one unit of work keyed by `correlation_key`,
aggregating the three service slices plus the event timeline. Stored in `work_items`.

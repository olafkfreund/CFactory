# CFactory
CFactory — the agentic control-tower cockpit over the PARR pipeline. One pane of glass + LLM copilot across PFactory, AIFactory and TFactory. Ports 3110/3111.

Recently shipped:

- **Live execution diagram** — an animated dependency-graph in the task-detail drawer that draws whichever PARR stage is furthest along (test, else code, else plan) from a shared `graph` field on `GET /api/workitems/{key}/process`. Done nodes turn green with a robot stamp, active nodes pulse cyan, failed shake red, stalled pulse amber; the edge the work is flowing along animates with marching dashes, and each node carries a live mm:ss timer.
- **Per-task cost and tokens** — a "Cost & tokens by task" panel in Mission Control plus a live cost/token stamp on running task cards, sourced from CFactory's own `/api/tokens` event store.
- **Per-worker observability** — per-worker / per-provider cost and token attribution, drilled down from `GET /api/tokens/by_worker`.
- **Requirement-to-test traceability** — a Traceability panel renders the `verification.traceability[]` matrix as an **acceptance-criterion x test x VAL x verdict** table: one row per AC showing its covering test(s), the Verification Assurance Level reached, and the pass/fail/not_run verdict. An AC with no covering test is flagged as a gap, not left blank. Backed additively on `GET /api/workitems/{key}/process` (RFC-0015).
- **Readable plan artifacts** — the human-readable spec / plan / tasks Markdown PFactory emits is shown as tabbed docs in the task drawer, so reviewers read the plan as prose rather than raw structures.
- **Cost estimate vs actual + routing rationale** — for one task, the pre-execution `cost_estimate_usd` and routing class/rationale (RFC-0014) shown next to the actual rolled-up spend, with estimate-vs-actual variance. A real `$` figure appears only for **metered** billing; subscription and local runs report tokens and time, never a notional dollar amount.
- **Running cost, not terminal-gated** — the cockpit records usage from any completion event that carries it, including the **non-terminal** snapshots PFactory and AIFactory emit, so a plan parked awaiting approval or a build still in flight reports what it has spent so far instead of showing `$0`.

See the [architecture docs](docs/architecture.md) for details, or the live cockpit docs at <https://cfactory.freundcloud.com>.

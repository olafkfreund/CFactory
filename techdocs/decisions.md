# Decisions

The architectural decisions that shaped CFactory. CFactory does not yet keep a
versioned `.agent-os/product/decisions.md`; this page is the Backstage-facing summary,
distilled from the code's design choices and the public
[roadmap](https://cfactory.freundcloud.com/roadmap/).

## DEC-001 — Read-first, advise-and-confirm cockpit (no autonomous writes)

> **Accepted** · Product / Safety

CFactory observes and correlates; it never writes to an upstream service on its own.
Every mutation is a two-step `propose → confirm`: a tool builds a `PreparedAction`
(target service · endpoint · payload · rationale), a human reviews it, and only an
explicit `POST /api/actions/execute` performs the write. This keeps the operator in
the loop and makes the cockpit safe to point at a live pipeline. See
`apps/backend/cfactory/actions.py`.

## DEC-002 — Consume each service's existing REST/WS surface, not its MCP server

> **Accepted** · Technical

Each Factory service ships a stdio MCP server, but that is spawned per-process by an
LLM client and is unsuited to a persistent, multi-user dashboard. CFactory instead
talks to each service's **existing** REST + WebSocket surface via per-service adapters,
absorbing schema drift with a dotted-key `first()` helper. See
`apps/backend/cfactory/adapters/`.

## DEC-003 — The WorkItem keyed by the GitHub issue number is the linchpin

> **Accepted** · Technical / Product

The family lacked a shared identity for a unit of work. CFactory introduces the
`WorkItem`, keyed by the GitHub issue number (synthetic fallback otherwise), threading
`plan → code → test` with per-service state slices and an ordered event timeline. This
is what lets the cockpit and copilot answer "where is feature X" with history. See
`apps/backend/cfactory/models.py` and RFC-0001.

## DEC-004 — Idempotent completion-event ingestion (RFC-0001)

> **Accepted** · Technical

`POST /api/events` (alias `/api/events/completion`) ingests the normalized RFC-0001
completion envelope and is idempotent by `(service, correlation_key, status)`: a
duplicate is accepted but is a no-op — no timeline append, no re-broadcast. The same
idempotency applies to upstream WebSocket messages, so a flapping feed never doubles
the board. See `apps/backend/cfactory/app.py` and `upstream_ws.py`.

## DEC-005 — Tamper-evident audit chain for confirmed actions

> **Accepted** · Technical / Governance

Every confirmed action is recorded with an HMAC-SHA256 hash chained to the prior
entry's hash, anchored by `CFACTORY_AUDIT_HMAC_SECRET`. Any after-the-fact mutation,
reordering or deletion breaks the chain (`AuditStore.verify`) — the same anchoring the
family uses for its enterprise audit trail, kept deliberately small here. See
`apps/backend/cfactory/audit.py`.

## DEC-006 — Local-first, with hosted/multi-tenant as additive seams

> **Accepted** · Product / Technical

CFactory is local-first in v1: scoped API keys are OPEN until configured, identity
resolves to a single `local` user, and the tenant is always `default`. The
enterprise concerns — SAML/SCIM IdP integration and per-tenant data isolation — ship
only as named, documented seams (`enterprise.py`'s `AuthProvider` Protocol and
`tenant_id_for`), so the hosted offering is an additive plug-in rather than a rewrite.

## DEC-007 — Token/cost is honestly "not instrumented yet"

> **Accepted** · Product

The RFC-0001 v1.1 `usage` block is additive and present only when a service instruments
it. CFactory aggregates whatever it gets and exposes an `instrumented` flag per service,
so the UI shows "not instrumented yet" honestly rather than implying zero cost. See
`copilot/tools.py::token_totals`.

> **Update (2026-06-05):** all three services now emit the `usage` block —
> AIFactory, plus PFactory (Plan) and TFactory (Test). The Tokens & cost page now
> shows real per-service, per-work-item spend across the whole pipeline. The
> `instrumented` flag and the honest "not instrumented yet" fallback remain for
> any service or run that emits no usage (e.g. a deterministic, LLM-free plan run).

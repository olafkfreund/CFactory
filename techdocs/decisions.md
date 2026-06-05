# Decisions

Product-specific architectural decisions for CFactory. Suite-level decisions live in the
[Factory program docs](https://factory.freundcloud.com/) — this page records the choices
that shaped *the cockpit*.

## CD-001 — Observe over REST + WebSocket + webhooks, not MCP-first

**Decision.** CFactory consumes each upstream service through its existing REST/WebSocket
API plus the RFC-0001 completion-event webhook — **not** through their stdio MCP servers.

**Why.** The stdio MCP servers are spawned per-process by an LLM client and are unsuited to
a persistent, long-running dashboard. REST/WS/webhooks already exist and fit an observer.
(`docs/architecture.md`; the suite-level ADR-008.)

## CD-002 — Pure consumer

**Decision.** CFactory owns no pipeline logic. It observes and correlates, and writes only
through human-confirmed actions.

**Why.** Keeps the cockpit decoupled and independently deployable, with a clean audit trail;
the pipeline stays owned by PFactory/AIFactory/TFactory. (`docs/architecture.md`,
`docs/index.md`.)

## CD-003 — Advise-and-confirm for every write

**Decision.** `propose` only builds a `PreparedAction`; a separate, explicit
`POST /api/actions/execute` (gated on the `write` scope, audited) is the only write path.
The copilot never executes autonomously.

**Why.** Matches the market's "Red Zone" guardrail norm and the EU AI Act human-oversight
requirement; keeps automation trustworthy. (`apps/backend/cfactory/actions.py`,
`docs/_posts/2026-06-04-introducing-cfactory.md`.)

## CD-004 — WorkItem keyed by the GitHub issue number

**Decision.** Correlate everything onto a `WorkItem` keyed by the GitHub issue number — the
durable, human-visible identity the family otherwise lacks — with a synthetic fallback.

**Why.** It is the artifact every stage already references and doubles as the audit anchor.
(`docs/architecture.md`, `apps/backend/cfactory/models.py`.)

## CD-005 — PostgreSQL in prod, SQLite for dev/test

**Decision.** The same SQLAlchemy JSON models run on SQLite (dev) and PostgreSQL (prod,
reusing AIFactory's data layer).

**Why.** Zero-setup local development; a shared, production-grade store when hosted.
(`apps/backend/cfactory/db.py`, `flake.nix`.)

## CD-006 — Tamper-evident HMAC-anchored audit chain

**Decision.** Every confirmed action is written to an HMAC-anchored hash chain, mirroring
AIFactory's audit approach.

**Why.** A plain hash chain can be recomputed forward by an attacker; the HMAC anchor makes
the trail tamper-evident — the evidence regulated environments need.
(`apps/backend/cfactory/audit.py`, migration `557faa62dcdc_audit_hmac_chain.py`.)

## CD-007 — Local-first, opt-in live integrations

**Decision.** The upstream WebSocket subscription (`CFACTORY_SUBSCRIBE_UPSTREAMS`) and live
progress poller (`CFACTORY_LIVE_PROGRESS`) are **off by default**; the keystore is *open*
when no API keys are configured.

**Why.** Dev and tests must not reconnect-loop against services that aren't running, and a
single-user local cockpit shouldn't need auth ceremony. (`config.py`, `auth.py`.)

## CD-008 — Copilot behind an injectable runner seam

**Decision.** The Claude Agent SDK call sits behind an `AgentRunner` abstraction; the live
runner lazily imports the SDK and reads `ANTHROPIC_API_KEY` only when actually invoked.

**Why.** The app imports and tests run with no network, no key and no SDK install; the model
is swappable via `CFACTORY_COPILOT_MODEL`. (`apps/backend/cfactory/copilot/service.py`.)

## CD-009 — Enterprise features as documented seams

**Decision.** Scoped API keys ship now; SAML/SCIM and multi-tenancy are present as
documented seams (`enterprise.py`) deferred to the hosted deployment.

**Why.** Keep the open local cockpit lean while leaving a clear, intentional path to the
family's enterprise stack. (`apps/backend/cfactory/enterprise.py`, `auth.py`.)

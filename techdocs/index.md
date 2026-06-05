# CFactory

**The agentic control-tower cockpit over the PARR pipeline.**

CFactory is the **Observe / Review** stage of the [Factory](https://factory.freundcloud.com/)
family — one pane of glass that threads a single unit of work across **PFactory**
(plan), **AIFactory** (code) and **TFactory** (test), with an **advise-and-confirm**
LLM copilot on top.

```
PFactory ──▶ AIFactory ──▶ TFactory          … all observed & steered by …  CFactory
 (Plan)        (Act)        (Verify)                                          (Cockpit)
```

CFactory is a **pure consumer**. It owns no pipeline logic: it observes the other
three services through their existing REST/WebSocket APIs plus an opt-in
[RFC-0001 completion-event](apis/events.md) webhook, correlates everything onto a
**WorkItem** keyed by the GitHub issue number, and only ever writes back through a
human-confirmed action.

## At a glance

| | |
|---|---|
| **Stage** | Observe / Review (the cockpit) |
| **Backend** | Python 3.13 · FastAPI (`cfactory.app:app`, v0.1.0) · port **3111** |
| **Frontend** | React 19 · TypeScript · Vite 6 · port **3110** |
| **Copilot** | Claude Agent SDK (default model `claude-sonnet-4-6`) |
| **Store** | SQLAlchemy + Alembic — SQLite (`~/.cfactory/cfactory.db`) in dev, PostgreSQL in prod |
| **Integration** | REST + WebSocket + RFC-0001 completion-event webhook (no shared DB) |
| **Write model** | Advise-and-confirm — the copilot proposes, a human confirms |
| **Dev env** | Nix flake (`cfactory-dev`) + `justfile` |

## What this documentation covers

- **[Architecture](architecture.md)** — the pure-consumer design, the WorkItem
  correlation model, adapters, the copilot, and how a completion event flows in.
- **[APIs](apis/index.md)** — the REST + WebSocket Web API, and the completion-event
  ingress that makes CFactory the family's observer.
- **[Dependencies](dependencies.md)** — backend and frontend dependency inventory.
- **[Decisions](decisions.md)** — the choices that shaped CFactory and why.
- **[Glossary](glossary.md)** — terms used here and across the suite.

## Where CFactory sits

In Backstage, CFactory's entities are Components of the **`factory-suite`** System
(Domain `factory`). It **provides** its own Web API + completion-event ingress and
**consumes** the PFactory, AIFactory and TFactory APIs — so the catalog graph shows
the whole pipeline converging on the cockpit.

# Dependencies

A deliberately small dependency surface — CFactory is an observer, not a platform.

## Backend

`apps/backend/requirements.txt` (Python 3.13):

| Package | Constraint | Role |
|---|---|---|
| `fastapi` | `>=0.115` | REST + WebSocket app (`cfactory.app:app`). |
| `uvicorn[standard]` | `>=0.34` | ASGI server (run with `--http h11 --ws wsproto`). |
| `pydantic` | `>=2.10` | `CompletionEvent`, `WorkItem` and request/response models. |
| `pydantic-settings` | `>=2.7` | `CFACTORY_`-prefixed config (`config.py`). |
| `httpx` | `>=0.28` | Sync adapters polling the upstream services. |
| `websockets` | `>=14.0` | Upstream WS subscriber (`upstream_ws.py`). |
| `wsproto` | `>=1.2` | WebSocket protocol for uvicorn (Upgrade-safe). |
| `psycopg[binary]` | `>=3.2` | PostgreSQL driver (production store). |
| `sqlalchemy` | `>=2.0` | ORM for the WorkItem store (`store.py`). |
| `alembic` | `>=1.14` | Schema migrations (`apps/backend/migrations/`). |
| `claude-agent-sdk` | `>=0.1.16` | The advise-and-confirm copilot runtime. |

The copilot reads `ANTHROPIC_API_KEY` from the environment at call time; the SDK import is
lazy so the app imports cleanly without it (and tests stay hermetic via the injectable
`AgentRunner` seam).

## Frontend

`apps/frontend-web/package.json`:

| Package | Version | Role |
|---|---|---|
| `react` / `react-dom` | 19 | The cockpit SPA. |
| `vite` | 6 | Dev server (port 3110) + build. |
| `@vitejs/plugin-react` | 4 | React plugin. |
| `typescript` | 5.7 | Types. |
| `framer-motion` | 12 | Live badges / board animations. |

There is no dedicated charting or component library — visualizations are hand-built CSS/SVG
(`src/index.css`, `src/icons.tsx`).

## Toolchain & runtime

- **Nix flake** (`flake.nix`, dev shell `cfactory-dev`): Python 3.13, Node 22, `uv`,
  PostgreSQL client, docker-client and the Nix linters. Exports the port/upstream/workspace
  env and provides `bootstrap-venv` (creates `apps/backend/.venv` via `uv`) and
  `cfactory-test`.
- **`justfile`**: `bootstrap`, `run`, `test`, `ui`/`ui-install`/`ui-build`,
  `db-upgrade`/`db-revision`, `fmt`/`lint`.
- **`Dockerfile`**: `python:3.13-slim`, non-root, serves `cfactory.app:app` on 3111.

## Data store

SQLAlchemy models on both backends: **SQLite** at `~/.cfactory/cfactory.db` for dev/test
(default when `CFACTORY_DATABASE_URL` is unset), **PostgreSQL** in production (reusing the
family's data layer). Migrations are managed with Alembic.

> Regenerate this inventory from the manifests when dependencies change; the
> `cfactory-web-api` OpenAPI spec is generated from the running app (see
> [APIs](apis/index.md)).

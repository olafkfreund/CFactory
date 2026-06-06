# Containerizing & deploying CFactory

CFactory deploys as **two pods**: the backend API and the cockpit UI (nginx),
behind one Ingress.

```
        Ingress (host: cfactory.local)
          /            \
   / (UI)              /api, /health, /api/ws
        │                     │
   ┌──────────┐        ┌──────────────┐
   │ frontend │ ──────▶│   backend    │
   │  nginx   │ /api   │ uvicorn :3111│
   │  :8080   │        └──────────────┘
   └──────────┘
```

The frontend nginx proxies `/api`, `/health`, and the `/api/ws` WebSocket to the
backend Service, so the browser talks to a single origin.

## Images

Two build paths are supported.

### Docker (recommended for production)

```sh
just image-backend      # ghcr.io/dataseeek/cfactory:dev        (Dockerfile, pip deps)
just image-frontend     # ghcr.io/dataseeek/cfactory-frontend:dev (apps/frontend-web/Dockerfile)
just image-all
# override: just REGISTRY=myrepo TAG=v1.2.3 image-all
```

- **Backend** (`./Dockerfile`): slim Python 3.13, non-root (uid 65532), `pip install -r requirements.txt` (includes `claude-agent-sdk`), `uvicorn … --http h11 --ws wsproto` on 3111.
- **Frontend** (`apps/frontend-web/Dockerfile`): builds the Vite bundle, serves it with `nginx-unprivileged` (uid 101, port 8080). `BACKEND_URL` (env) sets the proxy target; the Helm chart wires it to the backend Service.

### Nix (reproducible)

```sh
just nix-frontend        # nix build .#frontend-static  → the cockpit dist/
just nix-backend-image   # nix build .#backend-image    → docker load < result
```

> ⚠️ The Nix `backend-image` builds the full API from nixpkgs deps, **but
> `claude-agent-sdk` is not in nixpkgs** — so the live copilot (`/api/copilot/ask`)
> is unavailable in that image until the dep is packaged. The Docker backend
> image installs it via pip and is the recommended production image.

## Kubernetes (Helm)

```sh
just helm-lint
just helm-template                       # render for review
helm upgrade --install cfactory charts/cfactory \
  --namespace cfactory --create-namespace \
  --set image.tag=v1.2.3 \
  --set frontend.image.tag=v1.2.3 \
  --set ingress.enabled=true \
  --set ingress.host=cfactory.example.com \
  --set ingress.className=nginx
```

Key values (`charts/cfactory/values.yaml`):

| Value | Purpose |
|---|---|
| `image.*` / `frontend.image.*` | backend / cockpit images + tags |
| `frontend.enabled` | toggle the UI pod (default `true`) |
| `ingress.enabled` / `host` / `className` / `annotations` | one Ingress: `/`→UI, `/api`+`/health`→backend |
| `database.enabled` + `database.existingSecret` | `CFACTORY_DATABASE_URL` from a Secret |
| `apiKeys.enabled` + `apiKeys.existingSecret` | scoped `CFACTORY_API_KEYS` from a Secret |
| `config.{aifactory,pfactory,tfactory}ApiUrl` | upstream endpoints (editable at runtime too) |

For the WebSocket feed through nginx-ingress, raise the read timeout:
`--set ingress.annotations."nginx\.ingress\.kubernetes\.io/proxy-read-timeout"=3600`.

## Local dev

- **flake devShell** (direnv default): `nix develop` → `just run` (backend) + `just ui` (frontend).
- **devenv** (opt-in): `devenv up` runs backend + frontend + Postgres together.

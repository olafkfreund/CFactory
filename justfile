# CFactory task runner. Run `just` for the list.
# All recipes assume you're inside the Nix dev shell (`nix develop` / direnv).

set shell := ["bash", "-uc"]

_default:
    @just --list

# Create apps/backend/.venv and install backend + test deps (uses the flake fn).
bootstrap:
    bash -lc 'source <(declare -f bootstrap-venv 2>/dev/null); bootstrap-venv' || \
    uv venv apps/backend/.venv --python python3.13 && \
    uv pip install --python apps/backend/.venv/bin/python -r apps/backend/requirements.txt -r tests/requirements-test.txt

# Run the backend API (port from CFACTORY_BACKEND_PORT, default 3111).
run:
    PYTHONPATH=apps/backend apps/backend/.venv/bin/python apps/backend/run.py

# Run the backend test suite.
test *ARGS:
    PYTHONPATH=apps/backend apps/backend/.venv/bin/pytest -v {{ARGS}}

# Install frontend dependencies.
ui-install:
    cd apps/frontend-web && npm install

# Run the cockpit dev server (port 3110, proxies /api + /health to 3111).
ui:
    cd apps/frontend-web && npm run dev

# Type-check + production build of the cockpit.
ui-build:
    cd apps/frontend-web && npm run build

# Apply database migrations (uses CFACTORY_DATABASE_URL, else local SQLite).
db-upgrade:
    cd apps/backend && .venv/bin/alembic upgrade head

# Autogenerate a new migration: just db-revision "message".
db-revision MSG:
    cd apps/backend && .venv/bin/alembic revision --autogenerate -m "{{MSG}}"

# ── Containers & Kubernetes ──────────────────────────────────────────────

# Image registry/tag (override: just REGISTRY=myrepo TAG=v1 image-all).
REGISTRY := "ghcr.io/dataseeek"
TAG := "dev"

# Build the backend image (Dockerfile, includes claude-agent-sdk via pip).
image-backend:
    docker build -t {{REGISTRY}}/cfactory:{{TAG}} -f Dockerfile .

# Build the cockpit (frontend) image (nginx serving the Vite bundle).
image-frontend:
    docker build -t {{REGISTRY}}/cfactory-frontend:{{TAG}} apps/frontend-web

# Build both images.
image-all: image-backend image-frontend

# Reproducible Nix builds (alternative to Docker).
nix-frontend:
    nix build .#frontend-static
nix-backend-image:
    nix build .#backend-image   # docker load < result

# Render the Helm chart (ingress on) for review.
helm-template *ARGS:
    helm template cfactory charts/cfactory --set ingress.enabled=true {{ARGS}}

# Lint the Helm chart.
helm-lint:
    helm lint charts/cfactory

# Install/upgrade into the current kube-context (set image tags + ingress host).
helm-install NAMESPACE="cfactory":
    helm upgrade --install cfactory charts/cfactory \
      --namespace {{NAMESPACE}} --create-namespace \
      --set image.tag={{TAG}} --set frontend.image.tag={{TAG}}

# Format Nix files.
fmt:
    nixpkgs-fmt flake.nix

# Lint Nix files.
lint:
    statix check . && deadnix .

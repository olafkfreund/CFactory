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

# Format Nix files.
fmt:
    nixpkgs-fmt flake.nix

# Lint Nix files.
lint:
    statix check . && deadnix .

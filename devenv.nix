{ pkgs, lib, ... }:

# CFactory developer environment (devenv.sh). Opt-in alongside flake.nix:
#   devenv shell   → tools + venv + env
#   devenv up      → run backend + frontend + postgres together
#
# Canonical local port map (UI / API): AIFactory 3100/3101, TFactory 3102/3103,
# PFactory 3104/3105, CFactory 3110/3111.
{
  packages = with pkgs; [
    just
    kubectl
    kubernetes-helm
    git
    jq
  ];

  # Backend: Python 3.13 venv built from the pinned requirements (pip installs
  # everything, including claude-agent-sdk which is not in nixpkgs).
  languages.python = {
    enable = true;
    venv.enable = true;
    venv.requirements = ./apps/backend/requirements.txt;
  };

  # Frontend: Node 22 (Vite cockpit).
  languages.javascript = {
    enable = true;
    package = pkgs.nodejs_22;
    npm.enable = true;
  };

  # Local WorkItem correlation store.
  services.postgres = {
    enable = true;
    listen_addresses = "127.0.0.1";
    port = 5432;
    initialDatabases = [{ name = "cfactory"; }];
  };

  env = {
    PYTHONPATH = "apps/backend";
    CFACTORY_BACKEND_PORT = "3111";
    CFACTORY_FRONTEND_PORT = "3110";
    CFACTORY_AIFACTORY_API_URL = "http://localhost:3101";
    CFACTORY_PFACTORY_API_URL = "http://localhost:3105";
    CFACTORY_TFACTORY_API_URL = "http://localhost:3103";
    CFACTORY_DATABASE_URL = "postgresql://localhost:5432/cfactory";
  };

  # `devenv up` runs these together.
  processes = {
    backend.exec = ''
      python -m uvicorn cfactory.app:app \
        --host 0.0.0.0 --port "$CFACTORY_BACKEND_PORT" \
        --http h11 --ws wsproto --reload
    '';
    frontend.exec = ''
      cd apps/frontend-web && npm install && npm run dev -- --host
    '';
  };

  enterShell = ''
    echo "CFactory devenv — backend :$CFACTORY_BACKEND_PORT · frontend :$CFACTORY_FRONTEND_PORT · postgres :5432"
    echo "  devenv up   → backend + frontend + postgres"
  '';
}

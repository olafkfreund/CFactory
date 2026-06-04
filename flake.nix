{
  description = "CFactory — agentic control-tower cockpit over the PARR pipeline (PFactory · AIFactory · TFactory)";

  inputs = {
    # nixpkgs-unstable matches python313 + recent nodejs_22 (same pin as the sibling Factories)
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    systems.url = "github:nix-systems/default";
  };

  outputs =
    { self
    , nixpkgs
    , systems
    , ...
    }:
    let
      forEachSystem = nixpkgs.lib.genAttrs (import systems);
      mkDevShell = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        pkgs.mkShell {
          name = "cfactory-dev";

          # ────────────────────────────────────────────────────────────
          # Build inputs (packages on PATH inside the shell)
          # ────────────────────────────────────────────────────────────
          packages = with pkgs; [
            # Languages
            python313
            nodejs_22

            # Python tooling — uv backs `bootstrap-venv`
            uv

            # Core dev tools
            git
            gh           # GitHub CLI
            just         # Justfile runner
            ripgrep      # fast search in scripts
            jq           # JSON in shell scripts
            direnv       # auto-loading via .envrc

            # WorkItem correlation store (design: reuse the AIFactory Postgres layer).
            # Provides `psql` + libpq headers for psycopg builds. The server itself
            # runs on the host or in Docker; this is the client + build deps.
            postgresql

            # Container runtime — adapters/integration tests may shell out to `docker`.
            docker-client

            # Nix dev tooling (per global standards: LSP + linters)
            nixd
            nil
            statix
            deadnix
            nixpkgs-fmt

            # Native deps for Python C-extensions (psycopg, etc.)
            stdenv.cc.cc
            zlib
            libffi
            openssl
            pkg-config
          ];

          # ────────────────────────────────────────────────────────────
          # Environment
          # NOTE: literals here don't undergo bash expansion. Values needing
          # $HOME / $PWD interpolation are set in shellHook.
          # ────────────────────────────────────────────────────────────
          env = {
            CFACTORY_BACKEND_PORT = "3111";
            CFACTORY_FRONTEND_PORT = "3110";
            # A dev shell must not inherit a production NODE_ENV — otherwise
            # `npm install` omits devDependencies (vite, typescript, @types/*).
            NODE_ENV = "development";
          };

          # ────────────────────────────────────────────────────────────
          # Shell hook — env vars + helper functions for project scripts
          # ────────────────────────────────────────────────────────────
          shellHook = ''
            export CFACTORY_ROOT="$PWD"
            # Workspace dir defaults to ~/.cfactory; user-overridable via .env.
            export CFACTORY_WORKSPACE_ROOT="''${CFACTORY_WORKSPACE_ROOT:-$HOME/.cfactory}"

            # Default upstream service endpoints the adapters talk to.
            # Canonical local port map (see Factory#5): AIFactory 3101, PFactory 3102,
            # TFactory 3103 (moving off the 3102 collision), CFactory 3110/3111.
            export CFACTORY_AIFACTORY_API_URL="''${CFACTORY_AIFACTORY_API_URL:-http://localhost:3101}"
            export CFACTORY_PFACTORY_API_URL="''${CFACTORY_PFACTORY_API_URL:-http://localhost:3102}"
            export CFACTORY_TFACTORY_API_URL="''${CFACTORY_TFACTORY_API_URL:-http://localhost:3103}"

            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
              pkgs.stdenv.cc.cc.lib
              pkgs.zlib
              pkgs.libffi
              pkgs.openssl
            ]}:''${LD_LIBRARY_PATH:-}"

            # bootstrap-venv — create apps/backend/.venv + install backend deps.
            bootstrap-venv() {
              set -e
              cd "$CFACTORY_ROOT"
              if [ ! -f apps/backend/requirements.txt ]; then
                echo "apps/backend/requirements.txt not found yet — scaffold the backend first."
                return 1
              fi
              if [ -d apps/backend/.venv ]; then
                echo "venv already exists at apps/backend/.venv — leaving it alone."
                echo "  (rm -rf apps/backend/.venv to reinstall)"
                return 0
              fi
              echo "Creating apps/backend/.venv with Python 3.13 via uv …"
              uv venv apps/backend/.venv --python python3.13
              echo "Installing backend dependencies …"
              uv pip install --python apps/backend/.venv/bin/python \
                -r apps/backend/requirements.txt
              if [ -f tests/requirements-test.txt ]; then
                uv pip install --python apps/backend/.venv/bin/python \
                  -r tests/requirements-test.txt
              fi
              echo "Done."
            }

            # cfactory-test — run the backend pytest suite (non-SDK by default).
            cfactory-test() {
              if [ ! -x apps/backend/.venv/bin/pytest ]; then
                echo "venv missing — run \`bootstrap-venv\` first."
                return 1
              fi
              cd "$CFACTORY_ROOT"
              PYTHONPATH=apps/backend apps/backend/.venv/bin/pytest -v "$@"
            }

            export -f bootstrap-venv cfactory-test

            echo ""
            echo "  CFactory devshell  ──────────────────────────────"
            echo "    python  : $(python --version 2>&1)"
            echo "    node    : $(node --version 2>&1)"
            echo "    uv      : $(uv --version 2>&1 | head -1)"
            echo "    psql    : $(psql --version 2>/dev/null || echo 'n/a')"
            echo "    docker  : $(docker --version 2>/dev/null || echo 'daemon not running')"
            echo "  ───────────────────────────────────────────────────"
            echo "    backend  :$CFACTORY_BACKEND_PORT   frontend :$CFACTORY_FRONTEND_PORT"
            echo "    upstreams: ai=$CFACTORY_AIFACTORY_API_URL pf=$CFACTORY_PFACTORY_API_URL tf=$CFACTORY_TFACTORY_API_URL"
            echo "  ───────────────────────────────────────────────────"
            echo "    shell fns: bootstrap-venv  cfactory-test"
            echo "  ───────────────────────────────────────────────────"
            echo ""
          '';
        };
    in
    {
      devShells = forEachSystem (system: {
        default = mkDevShell system;
      });

      # `nix fmt` runs nixpkgs-fmt across the repo.
      formatter = forEachSystem (system:
        nixpkgs.legacyPackages.${system}.nixpkgs-fmt
      );
    };
}

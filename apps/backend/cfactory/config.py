"""Runtime configuration, sourced from CFACTORY_* environment variables.

The dev shell (flake.nix) exports sensible local defaults; production overrides
via real environment variables or a .env file.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CFACTORY_", env_file=".env", extra="ignore")

    # Server
    backend_port: int = 3111
    frontend_port: int = 3110

    # Workspace root for local state (mirrors the sibling Factories' ~/.<name>).
    workspace_root: str = "~/.cfactory"

    # Upstream service endpoints the adapters talk to.
    # Canonical local port map: AIFactory 3101, PFactory 3102, TFactory 3103.
    aifactory_api_url: str = "http://localhost:3101"
    pfactory_api_url: str = "http://localhost:3102"
    tfactory_api_url: str = "http://localhost:3103"

    # WorkItem correlation store (set when Postgres is wired in #6).
    database_url: str | None = None

    # Opt-in: connect to each upstream service's WebSocket on startup (#10).
    # Off by default so dev/tests don't reconnect-loop against down services.
    subscribe_upstreams: bool = False

    # Opt-in live progress (#v2 P3): poll PFactory/TFactory + subscribe AIFactory
    # progress, broadcasting {type:"progress"}. Off by default (siblings + Postgres).
    live_progress: bool = False
    poll_interval_seconds: float = 4.0

    # Agentic copilot (#13). Model id for the Claude Agent SDK; the SDK reads
    # ANTHROPIC_API_KEY from the environment.
    copilot_model: str = "claude-sonnet-4-6"

    # Scoped API keys (#20). Local-first: when empty/None, auth enforcement is
    # OPEN (single-user local mode). When set, requests must carry a known key
    # with the required scope. Format: "<key>:read,write;<key2>:read".
    api_keys: str | None = None

    # Multi-tenant mode (#23). Local-first: OFF by default, so tenant resolution
    # always yields the single "default" tenant (unchanged local behaviour). When
    # CFACTORY_MULTI_TENANT=true (hosted deploy), the tenant is resolved per
    # request from the "X-Tenant-Id" header (falling back to "default"). This is
    # the resolution seam + the flag that turns it on; per-tenant *data scoping*
    # of store/audit queries remains DEFERRED to the hosted deployment.
    multi_tenant: bool = False

    # HMAC secret anchoring the tamper-evident audit chain (#21). Each audit
    # entry's hash is HMAC-SHA256 over its canonical fields chained to the prior
    # entry's hash, so any after-the-fact mutation breaks the chain. The default
    # below is a CLEARLY-LABELLED dev secret: set CFACTORY_AUDIT_HMAC_SECRET to a
    # real secret in any hosted/shared deployment.
    audit_hmac_secret: str = "dev-insecure-audit-hmac-secret-change-me"

    def upstream_ws_urls(self) -> dict[str, str]:
        """Derive ws(s):// URLs for each service's live feed from its API URL."""
        def to_ws(url: str) -> str:
            ws = url.replace("https://", "wss://").replace("http://", "ws://")
            return ws.rstrip("/") + "/api/ws"

        return {
            "pfactory": to_ws(self.pfactory_api_url),
            "aifactory": to_ws(self.aifactory_api_url),
            "tfactory": to_ws(self.tfactory_api_url),
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

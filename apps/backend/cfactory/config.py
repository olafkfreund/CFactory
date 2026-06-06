"""Runtime configuration, sourced from CFACTORY_* environment variables.

The dev shell (flake.nix) exports sensible local defaults; production overrides
via real environment variables or a .env file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CFACTORY_", env_file=".env", extra="ignore")

    # Server
    backend_port: int = 3111
    frontend_port: int = 3110

    # Workspace root for local state (mirrors the sibling Factories' ~/.<name>).
    workspace_root: str = "~/.cfactory"

    # Upstream service endpoints the adapters talk to.
    # Canonical local port map (UI / API): AIFactory 3100/3101, TFactory 3102/3103,
    # PFactory 3104/3105, CFactory 3110/3111. Editable at runtime via the Services view.
    aifactory_api_url: str = "http://localhost:3101"
    pfactory_api_url: str = "http://localhost:3105"
    tfactory_api_url: str = "http://localhost:3103"

    # Service token for AIFactory's live agent console WebSocket (#34). When set,
    # the live-agents proxy sends it as `Authorization: Bearer <token>` to the
    # upstream rmux WS. Leave unset for local dev where AIFactory runs with
    # DISABLE_AUTH. The token stays server-side — it is never sent to the browser.
    aifactory_token: str | None = None

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
    copilot_model: str = "claude-opus-4-8"

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


# ── Editable upstream endpoints ──────────────────────────────────────────────
# The three upstream URLs can be edited at runtime from the Services view. Edits
# mutate the shared Settings singleton (so every consumer picks them up live) and
# are persisted to a small JSON file in the workspace, so they survive a restart.

EDITABLE_SERVICES = ("aifactory", "pfactory", "tfactory")


def _overrides_path(settings: Settings) -> Path:
    return Path(os.path.expanduser(settings.workspace_root)) / "service-endpoints.json"


def load_service_overrides(settings: Settings | None = None) -> Settings:
    """Apply any persisted endpoint overrides onto the settings instance."""
    settings = settings or get_settings()
    try:
        data = json.loads(_overrides_path(settings).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return settings
    if isinstance(data, dict):
        for name in EDITABLE_SERVICES:
            url = data.get(name)
            if isinstance(url, str) and url:
                setattr(settings, f"{name}_api_url", url)
    return settings


def set_service_url(name: str, url: str, settings: Settings | None = None) -> None:
    """Update one upstream endpoint at runtime and persist it. Raises
    ``ValueError`` on an unknown service or a malformed URL."""
    settings = settings or get_settings()
    if name not in EDITABLE_SERVICES:
        raise ValueError(f"unknown service: {name!r}")
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    setattr(settings, f"{name}_api_url", url)
    path = _overrides_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {n: getattr(settings, f"{n}_api_url") for n in EDITABLE_SERVICES}
    path.write_text(json.dumps(current, indent=2))

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


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

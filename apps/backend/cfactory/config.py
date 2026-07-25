"""Runtime configuration, sourced from CFACTORY_* environment variables.

The dev shell (flake.nix) exports sensible local defaults; production overrides
via real environment variables or a .env file.
"""

from __future__ import annotations

import json
import logging
import os
import warnings
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The clearly-labelled dev default for the audit-chain HMAC secret. Anchoring the
# tamper-evident audit chain on this in-repo value makes the chain forgeable by
# anyone who can read the source, so a hosted/shared deploy MUST override it via
# CFACTORY_AUDIT_HMAC_SECRET. See check_audit_secret() below (#81).
DEV_AUDIT_HMAC_SECRET = "dev-insecure-audit-hmac-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CFACTORY_", env_file=".env", extra="ignore", populate_by_name=True
    )

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

    # OpenObserve ("observe") — the OTLP/telemetry backend the fleet ships traces,
    # metrics and logs to. NOT a PARR factory: it has no completion-event API and is
    # never polled/hydrated into the WorkItem store. It appears ONLY in the Services
    # reachability view so the cockpit can show whether observe is up. Probed at its
    # real OpenObserve health path (GET /healthz → 200 "ok"). The default is the
    # local dev port; the in-cluster prod value is
    # http://observe.factory.svc.cluster.local:5080 — set CFACTORY_OBSERVE_API_URL in
    # the deployment manifest (don't hardcode the cluster DNS as the default).
    observe_api_url: str = "http://localhost:5080"

    # Shared bearer token for authenticated calls to the sibling factories. All
    # three (PFactory/AIFactory/TFactory) guard their REST + WS surface with the
    # same scheme — `Authorization: Bearer <APP_API_TOKEN>` — so the cockpit sends
    # this token on every adapter request, the live-progress poll, and each
    # upstream WS subscription. Leave unset for local dev where the factories run
    # with APP_DISABLE_AUTH=true. Stays server-side — never sent to the browser.
    upstream_token: str | None = None

    # Service token for AIFactory's live agent console WebSocket (#34). When set,
    # the live-agents proxy sends it as `Authorization: Bearer <token>` to the
    # upstream rmux WS. Falls back to `upstream_token` when unset. Leave both unset
    # for local dev where AIFactory runs with DISABLE_AUTH. The token stays
    # server-side — it is never sent to the browser.
    aifactory_token: str | None = None

    # AIFactory project a dispatched planning card is built in (RFC-0019 §3.2).
    # AIFactory's `/api/tasks/from-issue` requires a project_id and a card
    # carries none, so it is deployment config. Unset means low/medium cards
    # cannot be dispatched — the card is blocked with that reason rather than
    # silently accepted. `hard` cards route through PFactory and need no project.
    intake_project_id: str | None = None

    # GitHub card <-> issue sync (RFC-0019 §3.5, Phase 6). Sync is OFF unless
    # this is set, so an unconfigured deploy behaves exactly as before: no
    # network call on a card write, no issue opened. Server-side only.
    #
    # Deliberately does NOT accept the bare GITHUB_TOKEN / GH_TOKEN the way the
    # copilot accepts OLLAMA_API_KEY. Those are ambient on any machine with `gh`
    # logged in, and this credential OPENS ISSUES in someone's repo — inheriting
    # it would turn the feature on by accident for every developer, which is
    # exactly what happened the first time this was wired that way.
    github_token: str | None = None
    # The repository path a card's issue is OPENED in: "owner/repo" on GitHub,
    # "group/project" (subgroups allowed) on GitLab, "organization/project/repo"
    # on Azure DevOps. Adopting an existing issue needs no default — the card's
    # own issue_ref names its project. Unset means cards can only adopt, never
    # create.
    github_repo: str | None = None
    # Overridable for GitHub Enterprise (and pinned in tests).
    github_api_url: str = "https://api.github.com"

    # Which git host the board syncs cards with (RFC-0020 phase 1): "github"
    # (default), "gitlab" or "azure_devops". The default keeps every existing
    # deploy on exactly the behaviour it has today — the three settings below are
    # inert until this is changed.
    git_provider: str = "github"
    # Credential for the selected provider. The provider-neutral name for
    # `github_token` above: set either, this one wins. It carries the same
    # deliberate omission — the bare GITHUB_TOKEN / GH_TOKEN is NOT accepted,
    # because it is ambient wherever `gh` is logged in and this credential opens
    # issues in someone's repo.
    git_provider_token: str | None = None
    # Base URL of the provider instance — a self-hosted GitLab, an Azure DevOps
    # server, a GitHub Enterprise. Unset means the provider's public default
    # (`github_api_url` for GitHub, https://gitlab.com, https://dev.azure.com).
    git_provider_url: str | None = None

    # WorkItem correlation store (set when Postgres is wired in #6).
    database_url: str | None = None

    # Opt-in: connect to each upstream service's WebSocket on startup (#10).
    # Off by default so dev/tests don't reconnect-loop against down services.
    subscribe_upstreams: bool = False

    # Opt-in live progress (#v2 P3): poll PFactory/TFactory + subscribe AIFactory
    # progress, broadcasting {type:"progress"}. Off by default (siblings + Postgres).
    live_progress: bool = False
    poll_interval_seconds: float = 3.0

    # Agentic copilot (#13). Model id; meaning depends on copilot_provider below.
    copilot_model: str = "claude-opus-4-8"

    # Copilot LLM provider (#59). "claude" (default; Claude Agent SDK, reads
    # ANTHROPIC_API_KEY) or "ollama"/"openai_compatible" (any OpenAI-compatible
    # chat-completions endpoint — e.g. Ollama Cloud at https://ollama.com/v1).
    # When using Ollama Cloud, set copilot_model to a cloud model id (e.g.
    # "gpt-oss:120b") and supply ollama_api_key below.
    copilot_provider: str = "claude"

    # OpenAI-compatible endpoint for the copilot when copilot_provider != "claude".
    # base_url includes the /v1 suffix; the runner POSTs {base}/chat/completions
    # and the connectivity check GETs {base}/models. The key is sent as
    # `Authorization: Bearer <key>` and never leaves the backend. Accepts the bare
    # OLLAMA_API_KEY / OLLAMA_CLOUD_BASE_URL env (matching the shared secret) as
    # well as the CFACTORY_-prefixed forms.
    ollama_cloud_base_url: str = Field(
        default="https://ollama.com/v1",
        validation_alias=AliasChoices("CFACTORY_OLLAMA_CLOUD_BASE_URL", "OLLAMA_CLOUD_BASE_URL"),
    )
    ollama_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CFACTORY_OLLAMA_API_KEY", "OLLAMA_API_KEY"),
    )

    # Scoped API keys (#20). Local-first: when empty/None, auth enforcement is
    # OPEN (single-user local mode). When set, requests must carry a known key
    # with the required scope. Format: "<key>:read,write;<key2>:read".
    api_keys: str | None = None

    # Bearer token the MCP transport (POST /mcp) requires (#113). Treated as a
    # LEGACY FULL-SCOPE credential since RFC-0019 Phase 2a: a caller presenting it
    # holds both read and write. Existing prod clients keep working unchanged.
    # Routed through Settings so every secret flows through one typed boundary
    # rather than an inline os.environ read in mcp.py.
    mcp_secret: str | None = None

    # Explicit dev opt-in that re-opens /mcp when NO credential is configured
    # (RFC-0019 Phase 2a). Unconfigured used to mean "open"; it now means DENY, so
    # that adding write tools cannot silently expose mutation on a fail-open
    # surface. Set CFACTORY_MCP_DEV_OPEN=true for local dev only — never in a
    # hosted/shared deploy. Ignored once mcp_secret or api_keys is set.
    mcp_dev_open: bool = False

    # Public base URL of the token-gated API surface for editor/external clients
    # (#73 follow-up). When set (e.g. https://cfactory-api.freundcloud.org.uk —
    # the cloudflared host that reaches the backend directly, bypassing the
    # oauth2-proxy that fronts the browser cockpit), the /settings/token page shows
    # it next to the token so the operator knows where to point the editor. Display
    # only; leave unset for local/dev.
    public_api_url: str | None = None

    # Multi-tenant mode (#23). Local-first: OFF by default, so tenant resolution
    # always yields the single "default" tenant (unchanged local behaviour). When
    # CFACTORY_MULTI_TENANT=true (hosted deploy), the tenant is resolved per
    # request from the "X-Tenant-Id" header (falling back to "default") — the
    # header is injected by oauth2-proxy from the Keycloak ``tenant`` claim
    # (factory-gitops#13), never trusted from the browser directly. Work-item
    # reads/writes are scoped to the resolved tenant via store_dep (#172).
    multi_tenant: bool = False

    # HMAC secret anchoring the tamper-evident audit chain (#21). Each audit
    # entry's hash is HMAC-SHA256 over its canonical fields chained to the prior
    # entry's hash, so any after-the-fact mutation breaks the chain. The default
    # below is a CLEARLY-LABELLED dev secret: set CFACTORY_AUDIT_HMAC_SECRET to a
    # real secret in any hosted/shared deployment.
    audit_hmac_secret: str = DEV_AUDIT_HMAC_SECRET

    def upstream_ws_urls(self) -> dict[str, str]:
        """Derive ws(s):// URLs for each service's live feed from its API URL."""

        def to_ws(url: str) -> str:
            ws = url.replace("https://", "wss://").replace("http://", "ws://")
            # The upstream live feed is `/ws/events` on every service (PFactory,
            # AIFactory, TFactory all mount events_ws there). NOT `/api/ws` — that
            # is CFactory's OWN cockpit-facing endpoint (routes_ws.py); dialing it
            # upstream hits no route and Starlette 403s the WS, so the cockpit gets
            # zero pushed events and falls back to polling only (Mission Control
            # looks dead while task lists still populate). See routes_ws + the
            # services' server/websockets/events.py.
            return ws.rstrip("/") + "/ws/events"

        return {
            "pfactory": to_ws(self.pfactory_api_url),
            "aifactory": to_ws(self.aifactory_api_url),
            "tfactory": to_ws(self.tfactory_api_url),
        }


# The single implicit tenant in local/single-tenant mode (#172). Existing rows
# are backfilled to this value, and writers without a request context (poll
# loops, WS subscribers) stamp it.
DEFAULT_TENANT = "default"


def resolve_tenant(x_tenant_id: str | None = None, settings: Settings | None = None) -> str:
    """Resolve the tenant for a request (#23/#172).

    Multi-tenant OFF (local default): always ``DEFAULT_TENANT``, whatever headers
    say. ON: the ``X-Tenant-Id`` header value (injected by oauth2-proxy from the
    Keycloak tenant claim), falling back to ``DEFAULT_TENANT`` when absent/blank.
    """
    settings = settings or get_settings()
    if not settings.multi_tenant:
        return DEFAULT_TENANT
    return (x_tenant_id or "").strip() or DEFAULT_TENANT


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def is_local_only(settings: Settings) -> bool:
    """True when the cockpit is running in single-user local mode — i.e. neither
    scoped API keys (#20) nor multi-tenant mode (#23) are configured. In that
    posture the dev audit secret is acceptable; in any hosted/shared posture it
    is not (#81)."""
    return not (settings.api_keys or settings.multi_tenant)


def check_audit_secret(settings: Settings | None = None) -> bool:
    """Hard-warn at startup when the tamper-evident audit chain (#21) is still
    anchored on the in-repo dev default while NOT in local-only mode (i.e. API
    keys or multi-tenant are configured — a hosted/shared deploy). Leaving the
    default in place there makes the audit chain forgeable by anyone who knows
    the in-repo secret. Returns True when the dev default is in use in a
    non-local posture (the unsafe case), False otherwise."""
    settings = settings or get_settings()
    if settings.audit_hmac_secret == DEV_AUDIT_HMAC_SECRET and not is_local_only(settings):
        msg = (
            "INSECURE AUDIT SECRET: CFACTORY_AUDIT_HMAC_SECRET is still the "
            "in-repo dev default while API keys or multi-tenant mode are "
            "configured (hosted/shared posture). The tamper-evident audit chain "
            "is FORGEABLE — set CFACTORY_AUDIT_HMAC_SECRET to a real secret."
        )
        logger.error(msg)
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        return True
    return False


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


# ── Editable copilot settings ────────────────────────────────────────────────
# The copilot provider + model are editable at runtime from the Settings view.
# Edits mutate the Settings singleton and persist to a small JSON file (provider
# + model only — the API key is NEVER written to disk; it stays env/secret).

COPILOT_PROVIDERS = ("claude", "ollama")


def _copilot_settings_path(settings: Settings) -> Path:
    return Path(os.path.expanduser(settings.workspace_root)) / "copilot-settings.json"


def load_copilot_overrides(settings: Settings | None = None) -> Settings:
    """Apply any persisted copilot provider/model overrides onto the settings."""
    settings = settings or get_settings()
    try:
        data = json.loads(_copilot_settings_path(settings).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return settings
    if isinstance(data, dict):
        provider = data.get("provider")
        if isinstance(provider, str) and provider in COPILOT_PROVIDERS:
            settings.copilot_provider = provider
        model = data.get("model")
        if isinstance(model, str) and model:
            settings.copilot_model = model
    return settings


def set_copilot_settings(provider: str, model: str, settings: Settings | None = None) -> None:
    """Update the copilot provider + model at runtime and persist them. Raises
    ``ValueError`` on an unknown provider or empty model. The API key is not
    touched here — it is supplied via the environment/secret only."""
    settings = settings or get_settings()
    provider = (provider or "").strip().lower()
    if provider not in COPILOT_PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r} (expected one of {COPILOT_PROVIDERS})")
    model = (model or "").strip()
    if not model:
        raise ValueError("model must not be empty")
    settings.copilot_provider = provider
    settings.copilot_model = model
    path = _copilot_settings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"provider": provider, "model": model}, indent=2))

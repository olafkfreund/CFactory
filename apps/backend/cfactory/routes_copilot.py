"""Copilot Q&A plus the editable cockpit settings (copilot provider/model)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from .api_deps import AskRequest, CopilotSettingsUpdate, copilot_dep
from .auth import require_scope
from .config import COPILOT_PROVIDERS, get_settings, resolve_tenant, set_copilot_settings
from .copilot import Copilot, provider_status, reset_copilot
from .error_ref import InputRejectedError, client_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["copilot"])


def _settings_payload() -> dict[str, object]:
    settings = get_settings()
    status = provider_status(settings)
    return {"copilot": {**status, "providers": list(COPILOT_PROVIDERS)}}


@router.post("/api/copilot/ask")
async def copilot_ask(
    req: AskRequest,
    copilot: Annotated[Copilot, Depends(copilot_dep)],
) -> dict[str, object]:
    result = await run_in_threadpool(copilot.ask, req.question)
    return {"answer": result.answer, "work_items_considered": result.work_items_considered}


@router.get("/api/copilot/provider")
async def copilot_provider() -> dict[str, object]:
    """Active copilot LLM provider (#59). For an OpenAI-compatible endpoint
    (e.g. Ollama Cloud) this probes {base}/models so the cockpit can confirm
    connectivity and list the available cloud models."""
    settings = get_settings()
    return await run_in_threadpool(provider_status, settings)


@router.get("/api/settings")
async def get_settings_view(
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> dict[str, object]:
    """Editable cockpit settings: the copilot provider + model, plus the
    available providers and (for Ollama Cloud) live connectivity + model list.
    The API key is never returned — only a ``has_key`` flag.

    Also carries the caller's resolved `tenant`, which is how the cockpit knows
    which `/api/tenants/{tenant}/git-config` is its own: the tenant is injected
    by oauth2-proxy from the Keycloak claim and is deliberately not something the
    browser gets to choose, so the backend has to say what it resolved to.
    """
    payload = await run_in_threadpool(_settings_payload)
    return {**payload, "tenant": resolve_tenant(x_tenant_id, get_settings())}


@router.put("/api/settings/copilot")
async def update_copilot_settings(
    update: CopilotSettingsUpdate,
    _scope: Annotated[str | None, Depends(require_scope("write"))],
) -> dict[str, object]:
    """Switch the copilot provider/model at runtime. Persisted to the workspace
    (provider + model only — never the key) so it survives a restart, and
    effective immediately (the copilot is rebuilt on the next question).
    Requires the ``write`` scope when API keys are configured."""
    try:
        await run_in_threadpool(set_copilot_settings, update.provider, update.model)
    except InputRejectedError as exc:
        # set_copilot_settings raises InputRejectedError directly (#718) --
        # marked at the source, since only the raise site knows its message
        # is developer-written about the caller's own input.
        raise HTTPException(
            status_code=400, detail=client_error(logger, "invalid copilot settings", exc)
        ) from exc
    reset_copilot()
    return await run_in_threadpool(_settings_payload)

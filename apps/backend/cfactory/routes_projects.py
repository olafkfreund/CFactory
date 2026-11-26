"""Read-only tenant project catalog."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from .api_deps import cards_store_dep
from .auth import require_scope
from .cards import CardStore
from .projects import list_projects, resolve_project
from .routes_git_config import tenant_dep

router = APIRouter(tags=["projects"])


@router.get("/api/tenants/{tenant}/projects")
def projects_list(
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
) -> list[dict[str, Any]]:
    return list_projects(store)


@router.get("/api/tenants/{tenant}/projects/resolve")
def projects_resolve(
    ref: str,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
) -> dict[str, Any]:
    return resolve_project(store, ref)

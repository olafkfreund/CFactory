"""REST for read-only investigations (#561). Mounted only when the switch is not off."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import investigations
from .api_deps import action_transport_dep, audit_dep, cards_store_dep
from .audit import AuditStore
from .auth import KeyStore, keystore_dep, require_scope
from .card_ops import REST_ENDPOINT, AuditContext
from .cards import Card, CardStore
from .config import get_settings
from .enterprise import identity_dep

router = APIRouter(tags=["investigations"])


def _refuse(status: int, reason: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"reason": reason})


def _tenancy() -> None:
    if get_settings().multi_tenant:
        raise _refuse(HTTPStatus.CONFLICT, "multi_tenant_unsupported")


def investigation_write(op: str) -> Callable[..., None]:
    """Dependency for a write: write scope, a real key store, single tenant, and
    not draining (``create`` and ``dispatch`` only)."""

    def dependency(
        keystore: Annotated[KeyStore, Depends(keystore_dep)],
        _scope: Annotated[str | None, Depends(require_scope("write"))],
    ) -> None:
        if keystore.open:  # dev-open must never be able to start a run
            raise _refuse(HTTPStatus.FORBIDDEN, "keys_required")
        _tenancy()
        if op in ("create", "dispatch") and get_settings().investigations == "drain":
            raise _refuse(HTTPStatus.SERVICE_UNAVAILABLE, "investigations_draining")

    return dependency


def investigation_read(_scope: Annotated[str | None, Depends(require_scope("read"))]) -> None:
    _tenancy()


class CreateInvestigationCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=investigations.REQUEST_MAX)
    repository_id: int


@router.get("/api/investigations/context", dependencies=[Depends(investigation_read)])
def investigation_context(
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    actor: Annotated[str, Depends(identity_dep)],
    repository_id: int | None = None,
) -> dict[str, object]:
    try:
        return investigations.context(
            store,
            get_settings(),
            repository_id,
            AuditContext(audit, actor, endpoint=REST_ENDPOINT),
            transport,
        )
    except investigations.InvestigationError as exc:
        raise _refuse(exc.status, exc.reason) from None


@router.post("/api/investigations/cards", dependencies=[Depends(investigation_write("create"))])
def create_investigation_card(
    body: CreateInvestigationCard,
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    actor: Annotated[str, Depends(identity_dep)],
) -> Card:
    try:
        return investigations.create_card(
            store,
            AuditContext(audit, actor, endpoint=REST_ENDPOINT),
            get_settings(),
            body.request,
            body.repository_id,
            transport,
        )
    except investigations.InvestigationError as exc:
        raise _refuse(exc.status, exc.reason) from None

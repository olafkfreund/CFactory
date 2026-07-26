"""Tenant git configuration over HTTP (RFC-0020 §3.3).

Which git host a tenant's board syncs with, which project it opens issues in,
and which AIFactory project its builds land in — read, replaced and verified
here. Thin, like :mod:`cfactory.routes_cards`: the operations live in
:mod:`cfactory.git_config_ops` so the MCP twins are the same code rather than a
parallel implementation, and what is left is dependency wiring plus mapping
domain errors onto status codes.

**The tenant in the path is checked, not trusted.** ``{tenant}`` is a URL
segment a caller chooses; the tenant a caller may actually touch comes from the
resolved request identity (``X-Tenant-Id``, injected by oauth2-proxy from the
Keycloak claim — never from the browser). :func:`tenant_dep` refuses the request
when the two differ, so a tenant cannot read or write another tenant's git
configuration by typing its name into the URL. The path segment is kept because
the RFC specifies this shape and because it makes the resource identity explicit
in logs and audit entries — but it is an assertion the caller makes about
itself, and it is verified.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException

from . import git_config_ops
from .api_deps import action_transport_dep, audit_dep, cards_store_dep
from .audit import AuditStore
from .auth import require_scope
from .card_ops import AuditContext
from .cards import CardStore
from .config import get_settings, resolve_tenant
from .enterprise import identity_dep
from .git_config import GitConfigError, GitConfigUpdate

router = APIRouter(tags=["git-config"])

# Audit ``endpoint`` prefix for a mutation that arrived over REST. The MCP
# transport passes its own, so the trail says which surface was used.
REST_ENDPOINT = "/api/tenants"


def tenant_dep(
    tenant: str,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> str:
    """The tenant this request may operate on, or 403.

    Single-tenant mode resolves every request to ``default`` whatever the header
    says, so ``/api/tenants/acme/git-config`` is refused there too: the
    deployment has one tenant and ``acme`` is not it.
    """
    resolved = resolve_tenant(x_tenant_id, get_settings())
    if tenant != resolved:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=f"not your tenant: this request is scoped to {resolved!r}",
        )
    return resolved


@router.get("/api/tenants/{tenant}/git-config")
def get_git_config(
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
) -> dict[str, object]:
    """This tenant's git configuration (RFC-0020 §3.3).

    `status` is derived, never stored: `unconfigured` (no project named),
    `credential_missing` (a project, but no usable credential — credentials stay
    deployment-level until RFC-0020 phase 3), `configured` (reachable in
    principle, never proved), `verified` (proved by a `:verify` call).

    `source` is `stored` once the tenant has saved a configuration, and `env`
    while it is still falling back to the deployment's legacy environment
    variables. No credential is ever returned.
    """
    return git_config_ops.get_git_config(store)


@router.put("/api/tenants/{tenant}/git-config")
def put_git_config(
    # params are injected seams, not a call-site argument list.
    update: GitConfigUpdate,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Replace this tenant's git configuration.

    A full replacement, so an omitted optional field is CLEARED. Saving clears
    any recorded verification — it proved a configuration this one no longer is.

    400 when the provider is unknown, when a project path is not one the provider
    can address (`owner/repo`, or `organization/project/repo` on Azure DevOps),
    when `base_url` is not an http(s) origin, or when `default_labels` contains a
    `factory:<tier>` label — that label is the fleet's intake trigger, so putting
    it on an issue the board opens would build the same card twice.

    **No credential is accepted here.** The token stays deployment-level until
    RFC-0020 phase 3, exactly as the copilot settings persist provider and model
    but never the API key.
    """
    try:
        return git_config_ops.set_git_config(
            store, AuditContext(audit, actor, endpoint=REST_ENDPOINT), update
        )
    except GitConfigError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from None


@router.post("/api/tenants/{tenant}/git-config:verify")
def verify_git_config(
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Check that this configuration actually reaches its project.

    One cheap authenticated read of the repository — enough to prove the base URL
    resolves, the credential is accepted, and the project is visible to it. The
    outcome is recorded on the configuration, so `status` becomes `verified` or
    keeps the failure reason.

    Always 200: an unreachable host is `ok: false` with the reason, not a board
    error.
    """
    return git_config_ops.verify_git_config(
        store, AuditContext(audit, actor, endpoint=REST_ENDPOINT), transport=transport
    )

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
from pydantic import BaseModel, Field, SecretStr

from . import git_config_ops
from .api_deps import action_transport_dep, audit_dep, cards_store_dep
from .audit import AuditStore
from .auth import require_scope
from .card_ops import AuditContext
from .cards import CardStore
from .config import get_settings, resolve_tenant
from .credentials import CredentialError
from .enterprise import identity_dep
from .git_config import GitConfigError, GitConfigUpdate
from .git_connections import (
    GitConnectionCreate,
    GitConnectionUpdate,
    GitRepositoryCreate,
    GitRepositoryUpdate,
    GitResourceNotFoundError,
)
from .git_install import InstallError

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


class GitCredentialUpdate(BaseModel):
    """PUT body for the credential. One field, and it is write-only.

    Named ``credential`` rather than ``token`` to match the resource and the MCP
    twin — and because the discovery manifest is scanned for credential-shaped
    words, a guard worth not weakening for a naming preference.

    ``max_length`` is generous rather than tight: a GitHub App installation JWT
    is far longer than a PAT, and RFC-0020 §3.4 has this store holding whichever
    of them phase 4's install flow produces.

    ``SecretStr`` rather than ``str`` (Factory#377). A plain ``str`` here rendered
    the PAT verbatim in ``repr()``, ``str()``, ``model_dump()`` and
    ``model_dump_json()`` — and a FastAPI request model is exactly the object
    sitting in a traceback frame when the call below raises, so the credential
    landed in logs and error sinks. That contradicted RFC-0020 §3.4's own
    "never logged" guarantee at the one point where a user pastes a token.
    ``SecretStr`` masks all four surfaces; :meth:`~pydantic.SecretStr.get_secret_value`
    at the point of use is the only way back to the plaintext.
    """

    credential: SecretStr = Field(min_length=1, max_length=8192)


@router.put("/api/tenants/{tenant}/git-credential")
def put_git_credential(
    body: GitCredentialUpdate,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Store (or replace) this tenant's git credential (RFC-0020 §3.4).

    **Write-only.** The response is the masked indicator — `configured`, when it
    was stored, and which key wraps it — and there is no endpoint, anywhere, that
    returns the credential. It is encrypted before it is written (a per-record
    data key, itself wrapped by the deployment's `CFACTORY_CREDENTIAL_KEY`), and
    every later read of it appends an entry to the audit chain.

    503 when the deployment has no encryption key configured: with nothing to
    encrypt it with, the credential is REFUSED rather than written in the clear.
    That is a deployment problem, not a bad request, which is why it is a 5xx.
    """
    try:
        return git_config_ops.set_git_credential(
            store,
            AuditContext(audit, actor, endpoint=REST_ENDPOINT),
            body.credential.get_secret_value(),
        )
    except CredentialError as exc:
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=str(exc)) from None


@router.delete("/api/tenants/{tenant}/git-credential")
def delete_git_credential(
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Forget this tenant's git credential — the revocation path.

    Idempotent: removing one that is not there answers `removed: false` with a
    200, because the state the caller asked for is the state that now holds. The
    board keeps serving reads afterwards; its status simply becomes
    `credential_missing`.

    Operates on the connection the tenant's default repository lives on. To
    revoke a specific connection's credential, use
    `DELETE /api/tenants/{tenant}/git-connections/{connection_id}/credential`.
    """
    return git_config_ops.clear_git_credential(
        store, AuditContext(audit, actor, endpoint=REST_ENDPOINT)
    )


# ── Connections and repositories (RFC-0020 §3.3, phase 8) ────────────────────
#
# The two-level surface that replaces the single configuration above: a tenant has
# many CONNECTIONS (a provider + a host + a credential) and each connection has
# many REPOSITORIES (a project path + where it imports from + which AIFactory
# project it builds in). Exactly one repository is the tenant DEFAULT, and that is
# what a card which names no repository resolves to.
#
# Every one of these has an MCP twin in cfactory.mcp, and tests/test_board_parity.py
# fails the build if one is added without the other (RFC-0019 §3.3).


def _ctx(audit: AuditStore, actor: str) -> AuditContext:
    return AuditContext(audit, actor, endpoint=REST_ENDPOINT)


@router.get("/api/tenants/{tenant}/git-connections")
def list_git_connections(
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
) -> dict[str, object]:
    """Every git connection this tenant has, each with its repositories.

    A connection is a place the board can authenticate to: a provider
    (`github` / `gitlab` / `azure_devops`), a host, and a credential. A repository
    is something to work on through one — its project path, the project issues are
    imported from, and the AIFactory project its builds land in.

    `default_repository_id` is the repository a card that names none resolves to.
    `status` on each connection is derived, never stored: `unconfigured` (no
    repositories), `credential_missing` (no usable credential, or one the host
    refused), `configured` (reachable in principle, never proved), `verified`
    (proved by a `:verify` call). No credential is ever returned — only whether
    there is one, when it was stored and which key wraps it.

    Never 404s and never empty-errors: a tenant that has configured nothing gets
    an empty list.
    """
    return git_config_ops.list_git_connections(store)


@router.post("/api/tenants/{tenant}/git-connections")
def create_git_connection(
    body: GitConnectionCreate,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Add a git connection.

    400 when the provider is unknown, when `base_url` is not an http(s) origin, or
    when this tenant already has a connection to the same provider and host —
    configuring one host twice would leave "which credential reaches it?"
    ambiguous, so it is refused rather than duplicated.

    **No credential is accepted here.** Store it with
    `PUT /api/tenants/{tenant}/git-connections/{connection_id}/credential`, which
    is write-only.
    """
    try:
        return git_config_ops.create_git_connection(store, _ctx(audit, actor), body)
    except GitConfigError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from None


@router.patch("/api/tenants/{tenant}/git-connections/{connection_id}")
def update_git_connection(
    body: GitConnectionUpdate,
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Change a connection's provider, host or label.

    A patch: only the fields sent are applied. Changing the provider or the host
    CLEARS the verification (it proved a connection this one no longer is);
    changing only the label does not. The credential is untouched and stays valid —
    it is bound to the connection, not to its host.

    404 when this tenant has no such connection, 400 on a value the provider
    cannot use or a host this tenant already has a connection to.
    """
    try:
        return git_config_ops.update_git_connection(store, _ctx(audit, actor), connection_id, body)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None
    except GitConfigError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from None


@router.delete("/api/tenants/{tenant}/git-connections/{connection_id}")
def delete_git_connection(
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Forget a connection, **its repositories** and its credential.

    Everything that hangs off a connection goes with it: a repository cannot be
    reached without its host, and a credential for a connection that no longer
    exists is a secret kept for no reason. Cards are NOT deleted — a card whose
    repository is gone falls back to the tenant default. If the default was on
    this connection, the oldest remaining repository is promoted, so a tenant that
    still has repositories always has a default.

    404 when this tenant has no such connection.
    """
    try:
        return git_config_ops.delete_git_connection(store, _ctx(audit, actor), connection_id)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None


@router.post("/api/tenants/{tenant}/git-connections/{connection_id}:verify")
def verify_git_connection(
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Check that this connection's host answers and its credential is accepted.

    One cheap authenticated read of ONE of its repositories — the tenant default
    when that is on this connection, otherwise its oldest — which is enough to
    prove the host resolves, the credential works and the project is visible. The
    outcome is recorded on the connection, so its `status` becomes `verified` or
    keeps the failure reason.

    Always 200 for a reachability failure: an unreachable host is `ok: false` with
    the reason, not a board error. `ok: false` with `status: unconfigured` means
    the connection has no repositories to verify against yet. 404 when this tenant
    has no such connection.
    """
    try:
        return git_config_ops.verify_git_connection(
            store, _ctx(audit, actor), connection_id, transport=transport
        )
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None


@router.put("/api/tenants/{tenant}/git-connections/{connection_id}/credential")
def put_connection_credential(
    body: GitCredentialUpdate,
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Store (or replace) the credential for ONE connection (RFC-0020 §3.4).

    **Write-only.** The response is the masked indicator — `configured`, when it
    was stored, which key wraps it — and there is no endpoint, anywhere, that
    returns the credential. It is encrypted before it is written (a per-record data
    key, itself wrapped by the deployment's `CFACTORY_CREDENTIAL_KEY`) and sealed
    against **this tenant and this connection**, so the stored record cannot be
    replayed onto another connection even by someone holding the database. Every
    later read of it appends an entry to the audit chain.

    503 when the deployment has no encryption key configured: with nothing to
    encrypt it with, the credential is REFUSED rather than written in the clear.
    404 when this tenant has no such connection.
    """
    try:
        return git_config_ops.set_connection_credential(
            store, _ctx(audit, actor), connection_id, body.credential.get_secret_value()
        )
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None
    except CredentialError as exc:
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=str(exc)) from None


@router.delete("/api/tenants/{tenant}/git-connections/{connection_id}/credential")
def delete_connection_credential(
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Forget one connection's credential — the per-connection revocation path.

    Idempotent: removing one that is not there answers `removed: false` with a
    200. The connection keeps its repositories and simply reads as
    `credential_missing`. 404 when this tenant has no such connection.
    """
    try:
        return git_config_ops.clear_connection_credential(store, _ctx(audit, actor), connection_id)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None


@router.post("/api/tenants/{tenant}/git-connections/{connection_id}/install:start")
def start_git_install(
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Begin a GitHub App / GitLab OAuth install for this connection (RFC-0020 §3.4).

    Returns `authorize_url` — send a browser there. On GitHub it is the App's
    install page, where the human chooses **which repositories** the App may see;
    that choice is the reason an App is preferred over a pasted token, and it is
    not one this API makes for them. The URL carries a single-use `state` that
    expires in `expires_in_seconds`, and it is returned exactly once.

    Nothing is authenticated by this call and no credential is created: the
    connection stays as it was until the provider redirects back to the callback,
    which is where the state, the tenant binding and the provider's own answer are
    all checked.

    400 when this deployment has registered no app for the connection's provider
    (an operator supplies those as deployment configuration — see
    `docs/guides/git-app-install.md`), or when the provider has no install flow at
    all. **Azure DevOps is out of scope by design** and keeps the stored-credential
    path. 404 when this tenant has no such connection.
    """
    try:
        return git_config_ops.start_git_install(store, _ctx(audit, actor), connection_id)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None
    except InstallError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from None


@router.delete("/api/tenants/{tenant}/git-connections/{connection_id}/install")
def delete_git_install(
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Disconnect this connection's install — the revocation path for phase 4.

    Forgets the `installation_id`, any sealed refresh token and any cached
    short-lived token. Idempotent: disconnecting one that is not there answers
    `removed: false` with a 200. The connection keeps its repositories and reads
    as `credential_missing`.

    This does **not** uninstall the app at the provider — only the account owner
    can do that, on the provider's own settings page. 404 when this tenant has no
    such connection.
    """
    try:
        return git_config_ops.delete_git_install(store, _ctx(audit, actor), connection_id)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None


@router.get("/api/tenants/{tenant}/git-repositories")
def list_git_repositories(
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    _scope: Annotated[str | None, Depends(require_scope("read"))],
    connection_id: int | None = None,
) -> dict[str, object]:
    """This tenant's repositories as a flat list, newest connection last.

    Pass `connection_id` for just one connection's. `is_default` marks the one a
    card that names no repository resolves to.
    """
    return git_config_ops.list_git_repositories(store, connection_id)


@router.post("/api/tenants/{tenant}/git-connections/{connection_id}/repositories")
def create_git_repository(
    body: GitRepositoryCreate,
    connection_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Add a repository to a connection.

    The FIRST repository a tenant has becomes its default whatever `make_default`
    says — a tenant with repositories and no default would refuse every card that
    named none.

    400 when the project is not a path this connection's provider can address
    (`owner/repo`, or `organization/project/repo` on Azure DevOps), when it is
    already on this connection, or when `default_labels` contains a
    `factory:<tier>` label — that label is the fleet's intake trigger, so putting
    it on an issue the board opens would build the same card twice. 404 when this
    tenant has no such connection.
    """
    try:
        return git_config_ops.create_git_repository(store, _ctx(audit, actor), connection_id, body)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None
    except GitConfigError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from None


@router.patch("/api/tenants/{tenant}/git-repositories/{repository_id}")
def update_git_repository(
    body: GitRepositoryUpdate,
    repository_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Change a repository's project, intake project, AIFactory project or labels.

    A patch: only the fields sent are applied, and sending `null` for
    `intake_project` or `aifactory_project_id` CLEARS it. 404 when this tenant has
    no such repository, 400 on a value the provider cannot use.
    """
    try:
        return git_config_ops.update_git_repository(store, _ctx(audit, actor), repository_id, body)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None
    except GitConfigError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from None


@router.delete("/api/tenants/{tenant}/git-repositories/{repository_id}")
def delete_git_repository(
    repository_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Forget a repository.

    Cards that pointed at it are NOT deleted — they fall back to the tenant
    default, exactly like a card that never named one. If this was the default, the
    oldest remaining repository is promoted. 404 when this tenant has no such
    repository.
    """
    try:
        return git_config_ops.delete_git_repository(store, _ctx(audit, actor), repository_id)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None


@router.post("/api/tenants/{tenant}/git-repositories/{repository_id}:default")
def set_default_git_repository(
    repository_id: int,
    _tenant: Annotated[str, Depends(tenant_dep)],
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    _scope: Annotated[str | None, Depends(require_scope("write"))],
    actor: Annotated[str, Depends(identity_dep)],
) -> dict[str, object]:
    """Make this the tenant's default repository.

    The default is what a card that names no repository resolves to — for syncing
    an issue, for importing, and for which AIFactory project its build lands in —
    so this is the one setting that moves existing unassigned cards. A tenant has
    exactly one, enforced by the database. 404 when this tenant has no such
    repository.
    """
    try:
        return git_config_ops.set_default_git_repository(store, _ctx(audit, actor), repository_id)
    except GitResourceNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from None

"""Enterprise auth seams: identity, tenancy, and the pluggable auth provider.

CFactory is **local-first in v1** and designed to grow into a hosted, multi-tenant
deployment later. This module is the *seam* where the family's enterprise auth
(AIFactory's SAML/SCIM IdP integration and per-tenant data isolation) plugs in —
it deliberately ships only the local-mode behaviour plus clearly documented
extension points.

DEFERRED to the hosted deployment (#23 / Epic #4), NOT implemented here:

* **SAML / SCIM IdP integration.** Real SSO login, SCIM user/group
  provisioning, and group->scope mapping are provided by AIFactory's enterprise
  auth modules and wired in for the hosted offering. The :class:`AuthProvider`
  Protocol below is the contract a hosted provider implements; local v1 uses
  :class:`LocalAuthProvider`, which authenticates from the existing scoped API
  keys (:mod:`cfactory.auth`) and otherwise reports the single ``"local"`` user.
* **Per-tenant data isolation.** Hosted CFactory scopes every store/audit query
  by tenant. The :func:`tenant_id_for` seam is the single hook where that tenant
  resolution will live; in local v1 it always returns ``"default"`` and NO query
  is tenant-scoped yet (intentionally — wiring scoping into queries is part of
  the hosted step, not local v1).

Keeping these as named, documented seams means the hosted work is an additive
plug-in (a new :class:`AuthProvider` + tenant resolver) rather than a rewrite.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import Depends, Header, Request

from .auth import KeyStore, extract_key, keystore_dep
from .config import get_settings

# Identity used in local single-user mode (no IdP, no per-request identity).
LOCAL_IDENTITY = "local"

# Tenant used in local mode (and the fallback in multi-tenant mode when no
# tenant is supplied). Hosted multi-tenant scoping resolves a real tenant id
# via :func:`tenant_id_for`.
DEFAULT_TENANT = "default"

# Header carrying the tenant id when multi-tenant mode is enabled.
TENANT_HEADER = "X-Tenant-Id"


@runtime_checkable
class AuthProvider(Protocol):
    """Pluggable authentication provider — the SAML/SCIM extension point.

    Local v1 ships :class:`LocalAuthProvider`. A hosted deployment supplies an
    implementation backed by AIFactory's SAML/SCIM enterprise modules, mapping
    federated identities and SCIM-provisioned groups onto the same
    identity/scope contract used here.
    """

    def authenticate(self, request: Request) -> str:
        """Return the caller's identity for ``request``.

        Implementations resolve the principal from whatever credential the
        deployment uses (an API key locally; a SAML assertion / session in the
        hosted IdP integration). Returns :data:`LOCAL_IDENTITY` when there is no
        distinguishable caller.
        """

    def scopes_for(self, identity: str) -> set[str]:
        """Return the scopes granted to ``identity``.

        Locally these come from the scoped API-key store; in the hosted
        integration they are derived from SCIM group membership.
        """


class LocalAuthProvider:
    """Local-first :class:`AuthProvider`.

    Identity and scopes come from the existing scoped API-key store
    (:mod:`cfactory.auth`). When no keys are configured the keystore is OPEN and
    every caller is the single :data:`LOCAL_IDENTITY` user. There is no IdP,
    session, or SCIM provisioning — those arrive with the hosted provider.
    """

    def __init__(self, keystore: KeyStore) -> None:
        self._keystore = keystore

    def authenticate(self, request: Request) -> str:
        return identity_from_keystore(self._keystore, request)

    def scopes_for(self, identity: str) -> set[str]:
        # In local mode the "identity" is the API key itself (or LOCAL_IDENTITY
        # in OPEN mode). Resolve its scopes from the keystore.
        return self._keystore.scopes_for(identity) or set()


def identity_from_keystore(keystore: KeyStore, request: Request) -> str:
    """Derive the caller identity from the request's API key, else ``local``.

    When keys are configured and a known key is presented, the identity is that
    key (it is the principal in local mode). Unknown/absent keys, and OPEN mode,
    resolve to :data:`LOCAL_IDENTITY`.
    """
    if not keystore.configured:
        return LOCAL_IDENTITY
    key = extract_key(request.headers.get("authorization"), request.headers.get("x-api-key"))
    if key is not None and keystore.scopes_for(key) is not None:
        return key
    return LOCAL_IDENTITY


def tenant_id_for(request: Request) -> str:
    """Resolve the tenant for ``request`` — the multi-tenant isolation seam.

    Behaviour is gated by the ``CFACTORY_MULTI_TENANT`` flag (read via
    :func:`cfactory.config.get_settings`):

    * **OFF (default / local v1):** always returns :data:`DEFAULT_TENANT`,
      preserving single-tenant local behaviour. NO query is tenant-scoped.
    * **ON (hosted):** resolves the tenant from the :data:`TENANT_HEADER`
      (``X-Tenant-Id``) request header, falling back to :data:`DEFAULT_TENANT`
      when the header is absent or blank.

    This is the resolution seam plus the flag that turns it on. Threading the
    resolved tenant through store/audit queries for per-tenant data *isolation*
    remains DEFERRED to the hosted deployment (#23 / Epic #4); a hosted
    deployment may also replace this resolver to derive the tenant from the
    authenticated principal (SCIM org / IdP claim) instead of the header.
    """
    if not get_settings().multi_tenant:
        return DEFAULT_TENANT
    tenant = request.headers.get(TENANT_HEADER)
    if tenant and tenant.strip():
        return tenant.strip()
    return DEFAULT_TENANT


def identity_dep(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    keystore: KeyStore = Depends(keystore_dep),
) -> str:
    """FastAPI dependency returning the caller identity.

    Local v1: the presented API key when keys are configured, else
    :data:`LOCAL_IDENTITY`. Overridable in tests, and the hook a hosted
    :class:`AuthProvider` replaces. Used to stamp the audit ``actor``.
    """
    key = extract_key(authorization, x_api_key)
    if keystore.configured and key is not None and keystore.scopes_for(key) is not None:
        return key
    return LOCAL_IDENTITY


def tenant_dep(request: Request) -> str:
    """FastAPI dependency exposing :func:`tenant_id_for` as an injectable seam."""
    return tenant_id_for(request)

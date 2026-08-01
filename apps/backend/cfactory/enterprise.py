"""Caller-identity seam for the audit actor.

CFactory is local-first: identity comes from the existing scoped API keys
(:mod:`cfactory.auth`). :func:`identity_dep` is the FastAPI dependency that
resolves the caller — a non-reversible reference to the presented API key when
keys are configured, else the single ``"local"`` user — and is the hook a hosted
SSO/SCIM integration would override wholesale. Used to stamp the audit ``actor``.

An API key is a CLIENT, not a person, and every cockpit user shares one, so this
seam cannot answer "which human approved this" — it answers "which key was
presented". The trail says ``unattributed`` for exactly that reason; naming a
person requires threading a real end-user identity (the OIDC subject already
present at the oauth2-proxy in front of the cockpit) through this seam, which is
a separate change (#251).
"""

from __future__ import annotations

from fastapi import Depends, Header

from .auth import KeyStore, extract_key, key_actor, keystore_dep

# Identity used in local single-user mode (no IdP, no per-request identity).
LOCAL_IDENTITY = "local"


def identity_dep(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    keystore: KeyStore = Depends(keystore_dep),
) -> str:
    """FastAPI dependency returning the caller identity.

    Local v1: :func:`~cfactory.auth.key_actor` — an ``unattributed:key-<digest>``
    reference to the presented key — when keys are configured, else
    :data:`LOCAL_IDENTITY`. Overridable in tests, and the hook a hosted auth
    integration replaces. Used to stamp the audit ``actor``.

    This used to return the key ITSELF, which put a working write-scoped
    credential in the audit table and on screen in the cockpit's Audit view
    (#251). Never return ``key`` from here.
    """
    key = extract_key(authorization, x_api_key)
    if keystore.configured and key is not None and keystore.scopes_for(key) is not None:
        return key_actor(key)
    return LOCAL_IDENTITY

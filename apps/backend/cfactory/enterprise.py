"""Caller-identity seam for the audit actor.

CFactory is local-first: identity comes from the existing scoped API keys
(:mod:`cfactory.auth`). :func:`identity_dep` is the FastAPI dependency that
resolves the caller — the presented API key when keys are configured, else the
single ``"local"`` user — and is the hook a hosted SSO/SCIM integration would
override wholesale. Used to stamp the audit ``actor``.
"""

from __future__ import annotations

from fastapi import Depends, Header

from .auth import KeyStore, extract_key, keystore_dep

# Identity used in local single-user mode (no IdP, no per-request identity).
LOCAL_IDENTITY = "local"


def identity_dep(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    keystore: KeyStore = Depends(keystore_dep),
) -> str:
    """FastAPI dependency returning the caller identity.

    Local v1: the presented API key when keys are configured, else
    :data:`LOCAL_IDENTITY`. Overridable in tests, and the hook a hosted auth
    integration replaces. Used to stamp the audit ``actor``.
    """
    key = extract_key(authorization, x_api_key)
    if keystore.configured and key is not None and keystore.scopes_for(key) is not None:
        return key
    return LOCAL_IDENTITY

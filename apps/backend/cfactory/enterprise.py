"""Caller-identity seam for the audit actor.

:func:`identity_dep` is the FastAPI dependency that resolves WHO acted, and it
is what stamps the audit ``actor``. It answers with the best-attested identity
available, in this order:

1. ``user:<email>`` — a real PERSON, from an OIDC ID token whose signature this
   process verified against the issuer's JWKS (#251 part b).
2. ``unattributed:key-<digest>`` — a non-reversible reference to the presented
   API key (#251 part a). A key is a CLIENT, not a person, and every cockpit
   user shares one, so this says ``unattributed`` out loud rather than dressing
   a client up as an identity.
3. ``local`` — single-user mode, no IdP and no keys.

**Why a signed token and not a proxy header.** oauth2-proxy in front of the
cockpit knows the logged-in user, and the obvious cheap move is to have it
inject ``X-Auth-Request-Email`` and trust that. It would be wrong here: the
backend is ALSO reachable on a direct-to-backend host (the editor/MCP host,
guarded only by an API key), so a plaintext identity header can be typed by any
holder of a write-scoped key — a forged audit actor, which is strictly worse
than an honest ``unattributed``. The ID token is signed by the IdP, so
verifying it depends on nothing about which hop the request came over.

Residual, and deliberately not solved here: a captured, still-valid ID token
replayed on the direct host would name its subject. ``exp`` bounds that window;
closing it fully is perimeter work (Factory#312).
"""

from __future__ import annotations

import logging
from functools import lru_cache

import httpx
import jwt
from fastapi import Depends, Header

from .auth import KeyStore, extract_key, key_actor, keystore_dep
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

# Identity used in local single-user mode (no IdP, no per-request identity).
LOCAL_IDENTITY = "local"

# Prefix marking an actor that IS a named human, proved by a verified ID token.
# Distinct from `unattributed:` so a reader (and a SIEM rule) can tell "a person
# approved this" from "a shared client did" without guessing at the format.
USER_ACTOR_PREFIX = "user"

# Asymmetric only. Never include an HMAC alg here: with HS256 in the list, a
# token signed with the PUBLIC key as the HMAC secret verifies (the classic JWT
# algorithm-confusion forgery).
_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384")

# audit_entries.actor is String(128). Postgres REFUSES an over-long value, so an
# unbounded claim would turn a confirmed action into a 500 at the audit write —
# after the upstream call already happened. Cap below the column width. It also
# means no full token can ever occupy the field.
_ACTOR_MAX_LEN = 120

# First printable ASCII codepoint — anything below it is a control character.
_FIRST_PRINTABLE = 32

# MUST start with "Mozilla/5.0". The IdP is commonly behind Cloudflare, which
# 403s the default ``Python-urllib/3.x`` that PyJWKClient sends — and because
# every failure here degrades quietly to ``unattributed``, that 403 would look
# exactly like "no IdP configured" and the feature would never work while
# nothing went red. Verified against the live issuer: httpx's own UA passes,
# urllib's does not.
_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CFactory-audit-identity/1.0)"}


@lru_cache(maxsize=4)
def _discover_jwks_client(issuer: str) -> jwt.PyJWKClient:
    """Build a JWKS client for ``issuer``, once, via OIDC discovery.

    Discovery rather than a hardcoded ``/protocol/openid-connect/certs`` so the
    seam is not Keycloak-shaped — the same setting works for any OIDC provider.

    ``lru_cache`` only skips repeating DISCOVERY: PyJWKClient does its own key
    caching and rotation-aware refetch underneath. A raise is not cached, so a
    JWKS outage retries on the next request rather than pinning every later
    action to ``unattributed`` until a restart.
    """
    discovery = httpx.get(
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        headers=_FETCH_HEADERS,
        timeout=5.0,
    )
    discovery.raise_for_status()
    return jwt.PyJWKClient(discovery.json()["jwks_uri"], headers=_FETCH_HEADERS, timeout=5.0)


def _jwks_client(settings: Settings) -> jwt.PyJWKClient | None:
    """The JWKS client for the configured issuer, or None when there is no IdP."""
    if not settings.oidc_issuer:
        return None
    return _discover_jwks_client(settings.oidc_issuer)


def _claim_actor(claims: dict[str, object]) -> str | None:
    """Pick the human-resolvable claim and render it as the audit actor.

    ``email`` first because it is what an auditor or an access review resolves
    to a person; ``sub`` is the durable fallback when the IdP releases no email.

    The value is IdP-issued, but it lands in a field a human reads, so refuse
    anything with whitespace or control characters rather than letting a hostile
    claim forge a second column or smuggle a newline into an exported trail.
    """
    for name in ("email", "preferred_username", "sub"):
        value = claims.get(name)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or any(c.isspace() or ord(c) < _FIRST_PRINTABLE for c in value):
            continue
        return f"{USER_ACTOR_PREFIX}:{value}"[:_ACTOR_MAX_LEN]
    return None


def oidc_actor(id_token: str | None, settings: Settings) -> str | None:
    """Return ``user:<claim>`` for a VERIFIED ID token, else None.

    None means "no attested person", never "trust it anyway": every failure —
    unconfigured issuer, unreachable JWKS, bad signature, expired, wrong
    issuer/audience, no usable claim — falls back to the key-derived actor.
    An audit trail that names the wrong person is worse than one that admits it
    does not know.

    ``verify_aud`` is only enforced when an audience is configured; leaving it
    unset accepts any token from the issuer, which is the right default for a
    single-realm deployment and is documented as such on the setting.
    """
    if not id_token or not settings.oidc_issuer:
        return None
    # nginx forwards the whole header value, which is "Bearer <token>".
    token = id_token.removeprefix("Bearer ").removeprefix("bearer ").strip()
    if not token:
        return None
    try:
        client = _jwks_client(settings)
        if client is None:
            return None
        claims = jwt.decode(
            token,
            client.get_signing_key_from_jwt(token).key,
            algorithms=list(_ALGORITHMS),
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            options={
                "verify_aud": settings.oidc_audience is not None,
                "require": ["exp", "iss"],
            },
        )
    except Exception as exc:  # noqa: BLE001 - see below; must never 500 the action
        # Blind on purpose. Naming the caller is a LABEL on a request that is
        # already authorized, so no failure here — a bad signature, a JWKS
        # outage, a malformed header, a library raising something new after an
        # upgrade — may turn a confirmed HITL action into a 500 after the
        # upstream call has already happened. Every one of them means the same
        # thing: no attested person, fall back to the key reference.
        #
        # The exception TYPE only, never its message: a JWKS/HTTP error can
        # carry the URL and, for some libraries, the offending token — and that
        # token is a live credential for the SSO perimeter.
        logger.warning(
            "audit actor: ID token did not verify (%s); using key actor",
            type(exc).__name__,
        )
        return None
    return _claim_actor(claims)


def identity_dep(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_forwarded_id_token: str | None = Header(default=None, alias="X-Forwarded-Id-Token"),
    keystore: KeyStore = Depends(keystore_dep),
) -> str:
    """FastAPI dependency returning the caller identity for the audit actor.

    The named human wins when there is one: authorization is still the API key's
    job (``require_scope``), so by the time this runs the request is already
    allowed — the ID token only decides what the trail CALLS the caller.

    Overridable in tests, and the hook a hosted auth integration replaces.

    This used to return the key ITSELF, which put a working write-scoped
    credential in the audit table and on screen in the cockpit's Audit view
    (#251). Never return ``key`` from here.
    """
    user = oidc_actor(x_forwarded_id_token, get_settings())
    if user is not None:
        return user
    key = extract_key(authorization, x_api_key)
    if keystore.configured and key is not None and keystore.scopes_for(key) is not None:
        return key_actor(key)
    return LOCAL_IDENTITY

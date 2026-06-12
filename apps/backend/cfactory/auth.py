"""Scoped API-key authentication (#20).

Local-first, single-user posture: when NO keys are configured the keystore is
OPEN and every request is allowed. As soon as keys ARE configured, requests must
present a known key (via ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``)
that carries the required scope.

This reuses the family's scoped-key idea (AIFactory's ``acw_``-style keys with
``mcp:read``/``mcp:write`` scopes) but keeps it deliberately simple: bare
``read``/``write`` scopes, parsed from a single ``CFACTORY_API_KEYS`` env value.

Seam: the keystore is exposed through the ``keystore_dep`` FastAPI dependency and
the ``get_keystore()`` singleton, mirroring ``store_dep``/``get_store`` etc. Tests
override ``keystore_dep`` (preferred) or call ``set_keys()`` / ``reset_keystore()``.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from .config import get_settings

# Recognised scopes.
READ = "read"
WRITE = "write"


def parse_api_keys(raw: str | None) -> dict[str, set[str]]:
    """Parse the ``CFACTORY_API_KEYS`` string into ``{key: {scopes}}``.

    Format: ``"<key>:read,write;<key2>:read"`` — entries separated by ``;``,
    each entry is ``<key>:<comma-separated-scopes>``. Whitespace is tolerated and
    empty entries are skipped. A key with no scopes after the colon yields an
    empty scope set.
    """
    keys: dict[str, set[str]] = {}
    if not raw:
        return keys
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, _, scope_part = entry.partition(":")
        key = key.strip()
        if not key:
            continue
        scopes = {s.strip() for s in scope_part.split(",") if s.strip()}
        keys[key] = scopes
    return keys


class KeyStore:
    """A set of API keys mapped to the scopes each key holds.

    When empty (no keys configured) the store is in OPEN mode: ``authorize`` is a
    no-op and every request is permitted (local single-user default).
    """

    def __init__(self, keys: dict[str, set[str]] | None = None):
        self._keys: dict[str, set[str]] = dict(keys or {})

    @property
    def configured(self) -> bool:
        """True when at least one key is configured (enforcement is ON)."""
        return bool(self._keys)

    def scopes_for(self, key: str | None) -> set[str] | None:
        """Return the scope set for ``key``, or None if the key is unknown."""
        if key is None:
            return None
        return self._keys.get(key)

    def preferred_key(self) -> str | None:
        """Return a configured key suitable for handing to a client (#73).

        Prefers a key that carries WRITE (so the editor can act, not just read),
        then one with READ, then any configured key. Returns None in OPEN mode
        (no keys configured) — there is no token to hand out, and the API accepts
        every request anyway.
        """
        if not self._keys:
            return None
        for required in (WRITE, READ):
            for key, scopes in self._keys.items():
                if required in scopes:
                    return key
        # No scoped match — return the first configured key deterministically.
        return next(iter(self._keys))

    def authorize(self, key: str | None, scope: str) -> None:
        """Raise an HTTPException unless ``key`` may exercise ``scope``.

        OPEN mode (no keys configured) always allows. Otherwise: 401 for a
        missing/unknown key, 403 when the key lacks the required scope.
        """
        if not self.configured:
            return
        scopes = self.scopes_for(key)
        if scopes is None:
            raise HTTPException(
                status_code=401,
                detail="missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if scope not in scopes:
            raise HTTPException(
                status_code=403, detail=f"API key lacks required scope: {scope!r}"
            )


_keystore: KeyStore | None = None


def get_keystore() -> KeyStore:
    """Return the cached process-wide KeyStore, built from settings on first use."""
    global _keystore
    if _keystore is None:
        _keystore = KeyStore(parse_api_keys(get_settings().api_keys))
    return _keystore


def set_keys(keys: dict[str, set[str]]) -> None:
    """Test helper: replace the cached keystore with one holding ``keys``."""
    global _keystore
    _keystore = KeyStore(keys)


def reset_keystore() -> None:
    """Test helper: drop the cached keystore so it is rebuilt from settings."""
    global _keystore
    _keystore = None


def keystore_dep() -> KeyStore:
    """Dependency seam — overridden in tests with a configured KeyStore."""
    return get_keystore()


def _extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Pull the API key from an ``Authorization: Bearer`` header or ``X-API-Key``."""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    return None


def authorize_headers(
    keystore: KeyStore,
    authorization: str | None,
    x_api_key: str | None,
    scope: str,
) -> None:
    """Authorize a raw header pair against ``keystore`` for ``scope``.

    Shared by the HTTP key-enforcement middleware and the WebSocket handlers
    (neither can use the ``require_scope`` FastAPI dependency). In OPEN mode
    (no keys configured) this is a no-op. Raises ``HTTPException`` (401/403)
    otherwise — callers translate that into an HTTP response or a WS close.
    """
    if not keystore.configured:
        return
    key = _extract_key(authorization, x_api_key)
    keystore.authorize(key, scope)


def require_scope(scope: str):
    """Build a FastAPI dependency that enforces ``scope`` against the keystore.

    In OPEN mode (no keys configured) it allows the request and returns None.
    Otherwise it resolves the key from the request headers and authorizes it.
    """

    def dependency(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        keystore: KeyStore = Depends(keystore_dep),
    ) -> str | None:
        if not keystore.configured:
            return None
        key = _extract_key(authorization, x_api_key)
        keystore.authorize(key, scope)
        return key

    return dependency

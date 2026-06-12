"""Editor token hand-off for the one-click "Connect via Browser" flow (#73).

The ``factory-vscode`` extension opens ``/connect/vscode?redirect=<editor callback
URI>&state=<nonce>`` in the browser (where the operator is already on the cockpit),
and expects a ``302`` back to ``<redirect>?token=<token>&state=<state>`` so the
editor receives a working CFactory API token without any copy-paste.

This module is the pure, testable core: it validates the editor callback's scheme
against an allow-list (so a token can never be redirected to an attacker-controlled
``http(s)://`` / ``file://`` / ``javascript:`` target), and builds the final
redirect URL with the token and the echoed ``state`` appended.

CFactory has no per-user login — auth is a static set of scoped API keys
(``CFACTORY_API_KEYS``), and the token handed back is one of those keys (see
``KeyStore.preferred_key``). The redirect endpoint relies on the same network/ingress
gating that protects the rest of the cockpit UI; it is not itself key-guarded
(a browser session cannot present a bearer key).
"""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

# Allowed editor URI schemes. The extension derives the scheme from
# ``vscode.env.uriScheme``, which differs per editor / fork. Anything outside this
# set is rejected so a minted token can only ever land back in a real editor.
EDITOR_SCHEMES: frozenset[str] = frozenset(
    {
        "vscode",
        "vscode-insiders",
        "vscodium",
        "vscodium-insiders",
        "cursor",
        "windsurf",
        "trae",
        "positron",
    }
)


def validate_redirect(redirect: str | None) -> str | None:
    """Return an error reason if ``redirect`` is not a safe editor callback, else None.

    Safe means: present, parseable, and using one of ``EDITOR_SCHEMES``. The check is
    deliberately strict — an unknown scheme (including ``http``/``https``/``file``/
    ``javascript``/``data``) is rejected so the token is never handed to a non-editor.
    """
    if not redirect or not redirect.strip():
        return "missing redirect"
    try:
        parts = urlsplit(redirect)
    except ValueError:
        return "malformed redirect URI"
    scheme = parts.scheme.lower()
    if not scheme:
        return "redirect URI has no scheme"
    if scheme not in EDITOR_SCHEMES:
        return f"disallowed redirect scheme: {scheme!r}"
    return None


def build_redirect(redirect: str, token: str, state: str) -> str:
    """Append ``token`` and the echoed ``state`` to the editor callback URI.

    Preserves any query already present on ``redirect`` and percent-encodes the
    appended values. ``redirect`` must already have passed :func:`validate_redirect`.
    """
    parts = urlsplit(redirect)
    extra = urlencode({"token": token, "state": state})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

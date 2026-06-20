"""Editor hand-off: the connect-token view and the one-click ``/connect/vscode``
redirect.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import KeyStore, keystore_dep
from .config import get_settings
from .connect import build_redirect, validate_redirect

router = APIRouter(tags=["connect"])


@router.get("/api/settings/token")
def get_connect_token(
    keystore: Annotated[KeyStore, Depends(keystore_dep)],
) -> dict[str, object]:
    """The CFactory API token to use from an editor / external client (#73).

    Returns the configured key (the one ``/connect/vscode`` hands back) so the
    ``/settings/token`` page can show it with a copy button. ``configured`` is
    false in OPEN mode (no ``CFACTORY_API_KEYS`` set), where there is no token to
    copy and the API accepts every request anyway. Reachable behind the same
    gate as the rest of the cockpit UI."""
    settings = get_settings()
    token = keystore.preferred_key()
    return {
        "token": token or "",
        "configured": token is not None,
        "connect_url": settings.public_api_url or "",
    }


@router.get("/connect/vscode")
def connect_vscode(
    redirect: str,
    state: str,
    keystore: Annotated[KeyStore, Depends(keystore_dep)],
):
    """One-click editor hand-off (#73): 302 to ``<redirect>?token=…&state=…``.

    The ``factory-vscode`` extension opens this with the editor's callback URI
    and an opaque ``state`` nonce; we validate the callback's scheme against the
    editor allow-list, look up the configured API token, and redirect back with
    the token and the echoed ``state``. A disallowed scheme is rejected (400) and
    no token is issued. CFactory has no in-app login — access is gated at the
    ingress, so a reachable request is treated as authenticated."""
    reason = validate_redirect(redirect)
    if reason is not None:
        return HTMLResponse(
            f"<h1>Connection rejected</h1><p>{reason}.</p>"
            "<p>The redirect target must be a known editor URI "
            "(vscode://, vscodium://, cursor://, windsurf://, …).</p>",
            status_code=400,
        )
    if not state.strip():
        return HTMLResponse(
            "<h1>Connection rejected</h1><p>missing state nonce.</p>",
            status_code=400,
        )
    token = keystore.preferred_key() or ""
    return RedirectResponse(build_redirect(redirect, token, state), status_code=302)

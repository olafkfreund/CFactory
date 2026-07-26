"""The install callback the git provider redirects to (RFC-0020 §3.4, phase 4).

**CALLBACK HOSTING — the decision, stated where the handler is.** This endpoint
is served on ``https://cfactory-mcp.freundcloud.org.uk``, the host that already
bypasses oauth2-proxy for ``/mcp``. The alternative the RFC names — exempting a
path on ``https://cfactory.freundcloud.org.uk`` — would cut an unauthenticated
hole in the perimeter that fronts the entire cockpit, and a path exemption is
still a change to the thing protecting everything behind it. The MCP host needs
no perimeter change at all, so that is where it lives.

That choice does not make the endpoint safer; it makes the *perimeter* untouched.
The endpoint is exposed either way, because a provider redirect is a browser
navigation carrying no session, no API key and no ``X-Tenant-Id`` header. So the
security lives here, in the handler:

**What is verified.** Everything, before anything is written:

* the ``state`` matches a row stored as its **SHA-256** — the database never holds
  a presentable state, so reading it yields nothing usable;
* that row is **consumed in the same transaction that reads it**, so the identical
  callback URL replayed a second later matches nothing. This is what makes the
  state un-replayable, and it is a property of the delete, not of a timestamp;
* it **expires** (ten minutes), so a state left in a browser history or a proxy
  log is stale long before anyone finds it;
* the **tenant and the connection come from that row**, never from the request. A
  callback cannot name a tenant. That is the cross-tenant guarantee, and it is
  structural rather than a check that could be forgotten;
* the **provider is asked to confirm the redirect's own claim** — GitHub, whether
  the ``installation_id`` really is an installation of THIS App, asked with a JWT
  only the holder of the deployment's private key can sign; GitLab, by exchanging
  the ``code`` on a back-channel POST carrying the client secret. GitHub's setup
  redirect carries no signature to check — there is none in the protocol — so this
  round trip is what stands in for one.

**What an attacker who reaches this endpoint can do.** Send arbitrary query
parameters and receive a 400. They cannot forge a state (256 bits of CSPRNG, and
only its hash is stored), cannot replay one, cannot name a tenant, cannot read
anything back — the response is a static page, and no credential, installation or
configuration is ever rendered — and cannot make the deployment store a token,
because the only value an install may persist is decided by
:func:`~cfactory.git_install.persistable_secret`.

**What they can do WITH a live state** — that is, if they are the person who
started the install, or stole that person's redirect URL inside its ten-minute
life: attach an installation or an OAuth grant of their choosing to the connection
whose install they started. On GitHub the worst case is pointing a tenant's
connection at an installation the attacker controls, which grants CFactory access
to the ATTACKER's repositories, not the tenant's. It is a loss of integrity of the
tenant's own configuration, visible in the Settings panel (which shows the account
the install landed on) and in the audit chain — not a disclosure of anybody's
credential.
"""

from __future__ import annotations

import html
import logging
from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import git_config_ops
from .api_deps import action_transport_dep, audit_dep, cards_store_dep
from .audit import AuditStore
from .cards import CardStore
from .config import get_settings
from .git_install import CALLBACK_PATH, CallbackClaim, InstallError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["git-install"])


def _page(title: str, body: str, *, status: int) -> HTMLResponse:
    """A self-contained result page. No assets, no scripts, no redirect.

    Deliberately not a redirect back into the cockpit: this host is not the
    cockpit host, so a redirect would need a destination taken from configuration
    or — far worse — from the request, and a callback that redirects somewhere the
    caller influences is the classic open redirect. A page that says what happened
    and asks the user to switch back to their Settings tab costs one click and
    removes the whole class.
    """
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;margin:4rem auto;max-width:34rem;"
        "padding:0 1.5rem}h1{font-size:1.25rem}p{color:#444}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>",
        status_code=status,
    )


class CallbackQuery(BaseModel):
    """The query string a provider redirect arrives with. Entirely untrusted.

    A model rather than six parameters so the untrusted bundle stays one object
    all the way to :func:`~cfactory.git_config_ops.complete_git_install`, which is
    the only thing allowed to believe any of it.

    ``setup_action`` and ``error`` are the provider's narration ("install",
    "access_denied"); they are logged and never acted on beyond distinguishing a
    cancellation from an attempt. Every field is optional because a hostile caller
    supplies whatever it likes and the handler must answer rather than 422.
    """

    state: str = ""
    installation_id: str | None = None
    code: str | None = None
    setup_action: str | None = None
    error: str | None = None


@router.get(CALLBACK_PATH, include_in_schema=False)
def git_install_callback(
    store: Annotated[CardStore, Depends(cards_store_dep)],
    audit: Annotated[AuditStore, Depends(audit_dep)],
    transport: Annotated[httpx.BaseTransport | None, Depends(action_transport_dep)],
    query: Annotated[CallbackQuery, Query()],
) -> HTMLResponse:
    """Where GitHub / GitLab send the browser once the human has consented.

    Unauthenticated by necessity and by design — see the module docstring for what
    that does and does not permit. Sync rather than async so FastAPI runs it in a
    worker thread: it makes one blocking provider call, and blocking the event
    loop for it would stall every WebSocket the cockpit holds.

    ``include_in_schema=False`` because it is not part of the board's programmatic
    contract: it is a browser landing spot, it has no MCP twin by design (there is
    nothing for an agent to invoke), and publishing it in the OpenAPI document
    would advertise its parameters to no useful end.
    """
    if query.error:
        # The provider itself refused (the user pressed Cancel, most often).
        # Nothing to consume: no state is spent on a consent that never happened.
        return _page(
            "Install cancelled",
            "The provider did not complete the install, so nothing was changed. "
            "You can start it again from Settings > Git connections.",
            status=int(HTTPStatus.OK),
        )
    try:
        # Whatever tenant scope the request produced is DISCARDED inside:
        # complete_git_install re-scopes onto the tenant named by the state row.
        # This host is not behind oauth2-proxy, so an X-Tenant-Id header here is a
        # claim the browser made and nothing may act on it.
        completed = git_config_ops.complete_git_install(
            store,
            CallbackClaim(query.state, query.installation_id, query.code),
            settings=get_settings(),
            transport=transport,
        )
    except InstallError as exc:
        # The reason is shown because every one of them is actionable by the
        # person looking at it and none names a secret; a live state, a token or
        # an installation never appears in an InstallError message.
        logger.warning("install callback refused (setup_action=%s): %s", query.setup_action, exc)
        return _page("Install not completed", str(exc), status=int(HTTPStatus.BAD_REQUEST))
    except Exception as exc:
        # An unauthenticated endpoint must not return a stack trace or a
        # provider's error body to whoever reached it. The detail goes to the log;
        # the caller gets the generic answer.
        logger.exception("install callback failed unexpectedly")
        _audit(audit, store, ok=False)
        return _page(
            "Install not completed",
            f"Something went wrong completing the install ({type(exc).__name__}). "
            "Nothing was stored. Try again from Settings > Git connections.",
            status=int(HTTPStatus.BAD_REQUEST),
        )

    _audit(audit, store.scoped(completed.tenant), ok=True)
    where = f" on {completed.account}" if completed.account else ""
    return _page(
        "Connected",
        f"This board can now reach {completed.provider}{where}. "
        "Return to Settings > Git connections — the connection is authenticated, "
        "and a fresh short-lived token is minted for each call.",
        status=int(HTTPStatus.OK),
    )


def _audit(audit: AuditStore, store: CardStore, *, ok: bool) -> None:
    """Chain the completion into the same tamper-evident trail as every mutation.

    The actor is the PROVIDER rather than a person: nobody authenticated to
    CFactory to reach this endpoint, and recording a human's name here would put a
    claim in the audit chain that nothing verified.
    """
    audit.record(
        actor="git-provider-callback",
        kind="complete_git_install",
        correlation_key=f"tenant:{store.tenant}",
        target_service="git_provider",
        endpoint=CALLBACK_PATH,
        status_code=int(HTTPStatus.OK) if ok else 0,
        ok=ok,
    )

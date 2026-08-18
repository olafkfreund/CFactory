"""Correlation ids for failures whose detail must not reach the caller.

CodeQL `py/stack-trace-exposure` (CWE-209): four places in this backend turned a
caught exception into `f"{type(exc).__name__}: {exc}"` and handed that string to
the client - on the unauthenticated install-callback page, and as a card's
`github_sync_error` in the `:sync` response. That text is written by third-party
provider code and by the standard library, and it routinely names internal
detail the caller has no business seeing: a private-key path on disk
(`the GitHub App private key file '/etc/cfactory/gh.pem' could not be read`), an
internal hostname from a DNS failure, a library version, a driver's SQL.

Truncating it does not help - the leak is at the front of the string. The fix is
to send the caller a generic sentence plus a **correlation id**, and put the full
failure in the server log under that same id, so an operator answering "what
happened to my sync at 14:02?" runs one grep:

    ref = error_reference(logger, f"issue sync failed for {card_key}", exc)
    return {"ok": False, "reason": f"the provider call failed (reference {ref})"}

    # server log (one line, see error_reference on why the stack is escaped):
    # WARNING cfactory.github_sync issue sync failed for CARD-7 [ref=9f2c1ab04d3e]:
    #     ConnectError: [Errno -2] Name or service not known | Traceback (most
    #     recent call last):\n  File "...github_sync.py", line 88, ...
"""

from __future__ import annotations

import logging
import traceback
import uuid

from factory_common.logsafe import sanitize_log

__all__ = ["InputRejectedError", "client_error", "error_reference"]

#: Length of the id. 12 hex chars is 48 bits: far beyond collision range for a
#: log window an operator will ever grep, and short enough to read aloud.
_REF_CHARS = 12

#: Cap on the rendered stack. `sanitize_log`'s 2000-char default is sized for a
#: single identifier and would cut a real traceback in half; 20k holds a deep
#: async stack whole while still bounding a recursion-error dump.
_MAX_TRACEBACK = 20_000


class InputRejectedError(ValueError):
    """A validation failure whose message is DELIBERATELY safe to hand back.

    ``error_reference`` above solves the "I don't know if this text is safe"
    case: redact it, log it in full, hand back a correlation id. This class is
    the other half -- for the sites where the raiser already knows the message
    is developer-written, about the caller's own input, and names nothing this
    server holds. GitConfigError/GitResourceNotFoundError/CredentialError raise
    sites in this package are that shape ("base_url must start with http:// or
    https://", "no git connection 4 for this tenant") -- fixed, reviewable
    strings, never an inner exception's rendered text.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep
    working. The safe sentence lives in :attr:`client_message`, set once at
    construction -- never re-derived from ``str(exc)``, because
    ``BaseException.__str__`` renders ``args``, and ``args`` is written by
    every exception in the process, not just this one's raise sites.

    What this does NOT establish is that a raise site chose its wording
    wisely: ``InputRejectedError(f"failed: {inner}")`` launders ``inner``
    straight through. See git_install.py's ``InstallError`` for the case that
    makes this concrete -- the exact same exception TYPE has one raise site
    that names a private-key path on disk and others that don't, so marking
    a type safe here means verifying the *raise sites actually reachable from
    this call path*, not the type in general.
    """

    def __init__(self, client_message: str) -> None:
        super().__init__(client_message)
        self.client_message = client_message


def client_error(logger: logging.Logger, context: str, exc: BaseException) -> str:
    """Return a caller-safe message: ``exc``'s own text if it is an
    :class:`InputRejectedError`, otherwise ``context`` plus an
    :func:`error_reference`.

    The one-liner a route handler reaches for instead of ``detail=str(exc)``.
    """
    if isinstance(exc, InputRejectedError):
        # See InputRejectedError: developer-written text about the caller's
        # own input. Surfaced verbatim, and not worth a log record either --
        # a rejected field is validation working, not an incident.
        return exc.client_message
    return f"{context} (reference {error_reference(logger, context, exc)})"


def error_reference(logger: logging.Logger, context: str, exc: BaseException) -> str:
    """Log ``exc`` in full under a fresh correlation id; return just the id.

    The caller composes the client-facing sentence around the id, because what
    reads well on an install page ("Something went wrong completing the install")
    is not what reads well on a card ("the provider call failed"). What the
    caller must NOT do is put any part of ``exc`` in that sentence.

    ``context`` is interpolated into the log message and is sanitized: it usually
    carries a card key, project slug or tool name that arrived from a request.
    """
    ref = uuid.uuid4().hex[:_REF_CHARS]
    logger.warning(
        "%s [ref=%s]: %s | %s",
        sanitize_log(context),
        ref,
        # One sanitize_log per interpolated value, with no exceptions to
        # remember: `type(exc).__name__` used to be its own unsanitized `%s`.
        sanitize_log(f"{type(exc).__name__}: {exc}"),
        # The stack, rendered HERE and pushed through the sanitizer, rather than
        # handed to logging as `exc_info=`. Two reasons, and the second is why
        # `exc_info=exc` -- the obvious one-word fix -- is not what landed:
        #
        # 1. `exc_info=True` (what this was) means "call sys.exc_info()", which
        #    is populated only while an `except` block is unwinding. It has no
        #    relationship to `exc`, so a caller that stashed the exception and
        #    logged it later recorded "NoneType: None" where the stack should be
        #    and the correlation id led to a line with nothing under it.
        #    `traceback.format_exception(exc)` reads the object we were given,
        #    so it works from a task callback, a retry wrapper or a `finally`.
        #
        # 2. Whatever is handed to `exc_info=` is rendered by the logging module
        #    itself and NEVER passes through `sanitize_log`. So sanitizing the
        #    message while also passing `exc_info` logs the payload twice -- once
        #    escaped, once raw -- and the raw copy can carry a newline and forge
        #    a whole log line (CWE-117), undoing the escaping right next to it.
        #    Rendering it ourselves means exactly one copy and it is escaped.
        #
        # The fleet closes (2) at the formatter instead, by writing one JSON
        # object per line (AIFactory#1320). This backend configures no handlers
        # at all -- it inherits uvicorn's line-based stdout format -- so there is
        # no formatter here to hold that guarantee, and it has to hold in the
        # helper. Note the escaping costs readability: the stack arrives as one
        # line with literal \n. That is the trade this repo pays until it grows
        # a structured formatter, at which point drop this and use `exc_info=exc`.
        sanitize_log("".join(traceback.format_exception(exc)), max_length=_MAX_TRACEBACK),
    )
    return ref

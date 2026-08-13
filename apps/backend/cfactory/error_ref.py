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

    # server log:
    # WARNING cfactory.github_sync issue sync failed for CARD-7 [ref=9f2c1ab04d3e]:
    #     ConnectError: [Errno -2] Name or service not known
    #     Traceback (most recent call last): ...
"""

from __future__ import annotations

import logging
import uuid

from factory_common.logsafe import sanitize_log

#: Length of the id. 12 hex chars is 48 bits: far beyond collision range for a
#: log window an operator will ever grep, and short enough to read aloud.
_REF_CHARS = 12


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
        "%s [ref=%s]: %s: %s",
        sanitize_log(context),
        ref,
        type(exc).__name__,
        sanitize_log(str(exc)),
        exc_info=True,
    )
    return ref

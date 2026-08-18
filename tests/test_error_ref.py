"""``error_reference`` must put the stack in the log, and only in escaped form.

Two properties, and a naive fix satisfies exactly one of them:

* **Recoverability.** The caller gets a bare correlation id; the whole point is
  that the id leads an operator to the full failure. ``exc_info=True`` meant
  "call ``sys.exc_info()``", which is empty outside an ``except`` block, so a
  caller that stashed the exception and logged it later got ``NoneType: None``
  under the id (CFactory#376).
* **Unforgeability.** Whatever reaches ``exc_info=`` is rendered by the logging
  module and never passes through ``sanitize_log``. So the one-word fix
  ``exc_info=exc`` makes the stack appear *raw*, next to the escaped copy of the
  same text -- and a newline in it starts a new line in a line-based log
  (CWE-117). Fixing the first property is what makes the second one live.

Both are asserted on the rendered log **FILE LINES**, never on records and never
on "was sanitize_log called". Python emits exactly one ``LogRecord`` however
many newlines the message carries, so a record-counting test cannot see a forged
line at all and passes green against a deliberately broken sanitizer.

Mutation checks (both verified red):
  * ``exc_info=True`` back in place of the rendered traceback -> the
    recoverability tests fail (no ``Traceback`` under the id).
  * ``exc_info=exc`` instead of the sanitized render -> the forging tests fail
    (the payload is emitted twice and a forged line appears).
"""

from __future__ import annotations

import logging
import re

import pytest
from cfactory.error_ref import InputRejectedError, client_error, error_reference

REF = re.compile(r"^[0-9a-f]{12}$")

PRIVATE_KEY = "/etc/cfactory/gh.pem"
INTERNAL_HOST = "cfactory-db.internal"


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("cfactory.test.error_ref")


class _LineCollector(logging.Handler):
    """Collects rendered log FILE LINES, not records.

    The whole hazard is a second line appearing where the handler wrote one
    record, so the split has to happen after formatting -- which is exactly what
    a file or stdout handler does.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.extend(self.format(record).splitlines())


def _emit(logger: logging.Logger, exc: BaseException) -> tuple[str, list[str]]:
    """Run ``error_reference`` and return ``(ref, rendered log file lines)``."""
    handler = _LineCollector()
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    try:
        ref = error_reference(logger, "issue sync failed", exc)
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    return ref, handler.lines


def _raised(exc: BaseException) -> BaseException:
    """Give ``exc`` a real ``__traceback__``, then leave the except block.

    Constructing the exception is not enough for the interesting case: the bug
    is specifically about logging an exception whose handler has already exited,
    which is where ``sys.exc_info()`` is empty.
    """
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 - re-handing it to the caller
        return caught


# ---------------------------------------------------------------------------
# Recoverability: the id must lead to the stack (CFactory#376)
# ---------------------------------------------------------------------------


def test_the_stack_reaches_the_log_outside_an_except_block(
    logger: logging.Logger,
) -> None:
    """RED on unfixed dev.

    ``exc_info=True`` reads ambient state that is empty here, so the record said
    ``NoneType: None`` where the stack should be.
    """
    exc = _raised(ConnectionRefusedError(f"cannot reach {INTERNAL_HOST}"))
    assert exc.__traceback__ is not None  # the object still carries it...

    ref, lines = _emit(logger, exc)  # ...but we are no longer in the handler
    blob = "\n".join(lines)

    assert REF.match(ref)
    assert f"[ref={ref}]" in blob, "the id must be on the record it identifies"
    assert "Traceback (most recent call last):" in blob, f"no stack logged: {blob!r}"
    assert "_raised" in blob, "the raising frame is missing from the stack"
    assert "NoneType: None" not in blob
    assert INTERNAL_HOST in blob, "the detail was not logged"


def test_the_stack_reaches_the_log_for_a_never_raised_exception(
    logger: logging.Logger,
) -> None:
    """A constructed exception has no ``__traceback__`` at all.

    There is no stack to render, but the type and message must still land -- not
    ``NoneType: None``.
    """
    _, lines = _emit(logger, ValueError(PRIVATE_KEY))
    blob = "\n".join(lines)

    assert "ValueError" in blob
    assert PRIVATE_KEY in blob
    assert "NoneType: None" not in blob


def test_two_failures_get_different_references(logger: logging.Logger) -> None:
    first, _ = _emit(logger, ValueError("a"))
    second, _ = _emit(logger, ValueError("b"))
    assert first != second


# ---------------------------------------------------------------------------
# Unforgeability: CWE-209 must not be traded for CWE-117
# ---------------------------------------------------------------------------

FORGED = "CRITICAL:cfactory.audit:tenant deleted by admin"


def test_a_newline_in_the_stack_cannot_forge_a_log_line(
    logger: logging.Logger,
) -> None:
    """The payload rides in the exception message.

    So it appears both in the rendered stack and in the head field -- every
    route it takes to the sink has to be escaped, not just the one that was
    wrapped first.
    """
    exc = _raised(ValueError(f"bad ref\n{FORGED}"))
    _, lines = _emit(logger, exc)

    assert not any(line == FORGED for line in lines), f"a forged log line was emitted: {lines!r}"
    assert len(lines) == 1, f"one record must render as one line, got {lines!r}"
    assert any(FORGED in line for line in lines), (
        "the payload must stay readable -- escaped, not stripped"
    )


def test_the_payload_is_never_emitted_unescaped(logger: logging.Logger) -> None:
    """Count, do not grep.

    ``exc_info=exc`` would emit the payload TWICE: once escaped in the message,
    once raw in logging's own render of the exception. A bare
    ``FORGED in blob`` assertion passes in both worlds. What distinguishes them
    is how many *unescaped* copies exist, so that is what is counted.
    """
    exc = _raised(ValueError(f"bad ref\n{FORGED}"))
    _, lines = _emit(logger, exc)
    blob = "\n".join(lines)

    assert blob.count(f"\n{FORGED}") == 0, (
        f"an unescaped copy of the payload reached the log: {blob!r}"
    )
    # Escaped copies: the head "Type: message" field and the rendered stack's
    # trailing "ValueError: ..." line. Both escaped; pinned so that dropping one
    # route (or adding a third, unescaped one) is a failure rather than a shrug.
    escaped = blob.count(f"\\n{FORGED}")
    assert escaped == 2, f"expected exactly 2 escaped copies, got {escaped} in {blob!r}"


def test_a_carriage_return_cannot_forge_a_line_either(
    logger: logging.Logger,
) -> None:
    """``\\r`` alone re-writes a terminal line and separates records for some
    shippers; ``sanitize_log`` escapes it, so it must not survive raw."""
    _, lines = _emit(logger, _raised(ValueError(f"bad\r{FORGED}")))
    blob = "\n".join(lines)

    assert "\r" not in blob
    assert f"\\r{FORGED}" in blob


# ---------------------------------------------------------------------------
# The boundary itself still holds
# ---------------------------------------------------------------------------


def test_the_returned_message_carries_no_internal_detail(
    logger: logging.Logger,
) -> None:
    message = client_error(logger, "issue sync failed", ValueError(PRIVATE_KEY))
    assert PRIVATE_KEY not in message
    assert "issue sync failed" in message
    assert "reference " in message


def test_a_rejected_field_reads_the_attribute_not_str(logger: logging.Logger) -> None:
    """``str(exc)`` renders ``args``, which anything can write.

    ``client_message`` has exactly one writer.
    """
    rejected = InputRejectedError("base_url must start with http:// or https://")
    rejected.args = (f"SOMETHING ELSE ENTIRELY {PRIVATE_KEY}",)
    assert client_error(logger, "ctx", rejected) == ("base_url must start with http:// or https://")

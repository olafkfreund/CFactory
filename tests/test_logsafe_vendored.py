"""The vendored log sanitizer must stop a forged log record (CWE-117).

CodeQL reported four ``py/log-injection`` sinks in the backend: a service name
from a settings write, an MCP tool name from a JSON-RPC body, and a project /
target from an issue import. A newline in any of them writes the attacker's own
record into the server log.

The fix wraps those values in ``factory_common.logsafe.sanitize_log`` (the hub
canonical, vendored at ``apps/backend/factory_common/``). This test is the
behaviour lock: it drives a real ``logging`` handler and asserts the forged
record is NOT emitted. Break the sanitizer - let a raw newline through - and it
goes red.
"""

from __future__ import annotations

import io
import logging

from factory_common.logsafe import sanitize_log

# What an attacker puts in an MCP tool name to forge an audit record.
FORGED_TOOL = "board.list\nWARNING:cfactory.audit:scope grant approved by admin"


def _emit(tool_name: str) -> list[str]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger = logging.getLogger("cfactory.tests.logsafe")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info("[cfactory-mcp] tool call failed tool=%s", tool_name)
    handler.flush()
    return stream.getvalue().splitlines()


def test_raw_value_forges_a_record() -> None:
    """Precondition: without the sanitizer the attack works."""
    records = _emit(FORGED_TOOL)
    assert len(records) == 2
    assert records[1] == "WARNING:cfactory.audit:scope grant approved by admin"


def test_sanitized_value_cannot_forge_a_record() -> None:
    records = _emit(sanitize_log(FORGED_TOOL))
    assert len(records) == 1
    assert not any(r.startswith("WARNING:cfactory.audit:") for r in records)
    # The payload survives, inert and greppable - debuggability preserved.
    assert records[0].endswith(
        "tool=board.list\\nWARNING:cfactory.audit:scope grant approved by admin"
    )


def test_real_values_are_not_mangled() -> None:
    """A sanitizer that ruins normal log lines is worse than the alert."""
    for value in ("board.list_cards", "acme/widgets", "CARD-42", "aifactory"):
        assert sanitize_log(value) == value

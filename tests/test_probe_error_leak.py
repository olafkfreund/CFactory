"""CWE-209: an httpx failure must not put an internal endpoint in the response (#382).

Four places turned a caught ``httpx.HTTPError`` straight into a response field:

* ``api_deps.fetch_provider_auth`` -> ``{"error": str(exc)}``
* ``api_deps.probe_observe``       -> ``ServiceProbe(detail=str(exc))``
* ``adapters.base`` probe          -> ``ServiceProbe(detail=str(exc))``
* ``actions`` chain                -> ``{"error": str(exc)}``

An httpx error renders the URL it could not reach, so the cockpit rendered the
internal service host and port to whoever could see the page. The probe detail
is the sharpest of the four: it exists to be displayed.

A fifth was subtler. ``routes_events`` catches ``AdapterError``, which looks
repo-owned and safe — but ``adapters/base.py`` builds it as
``f"{service}: GET {path} failed: {exc}"`` around the inner httpx error, so a
friendly-looking type was laundering the upstream host through.

Mutation check: restore any ``str(exc)`` and the matching case goes red naming
the fragment that escaped.
"""

from __future__ import annotations

import httpx
import pytest
from cfactory.api_deps import fetch_provider_auth, probe_observe

INTERNAL = "http://pfactory.factory.svc.cluster.local:8080"
LEAKS = ("pfactory.factory.svc.cluster.local", "8080", "ConnectError")


def _refusing_transport() -> httpx.MockTransport:
    """A transport that fails the way an unreachable service fails."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"[Errno 111] Connection refused to {request.url}", request=request
        )

    return httpx.MockTransport(handler)


def _assert_no_leak(text: str) -> None:
    for fragment in LEAKS:
        assert fragment not in text, f"leaked {fragment!r}: {text!r}"


def test_a_provider_auth_failure_does_not_name_the_endpoint() -> None:
    result = fetch_provider_auth(INTERNAL, transport=_refusing_transport())

    # Pinned: a result that never entered the except branch would report
    # reachable=True and the leak assertion below would prove nothing.
    assert result["reachable"] is False, f"handler not reached: {result!r}"
    _assert_no_leak(str(result["error"]))


def test_the_provider_auth_failure_still_carries_a_reference() -> None:
    result = fetch_provider_auth(INTERNAL, transport=_refusing_transport())
    assert "reference " in str(result["error"]), (
        f"nothing for the operator to grep: {result['error']!r}"
    )


def test_an_observe_probe_failure_does_not_name_the_endpoint() -> None:
    probe = probe_observe(INTERNAL, transport=_refusing_transport())

    assert probe.online is False, f"handler not reached: {probe!r}"
    assert probe.status == "offline"
    _assert_no_leak(str(probe.detail))
    assert "reference " in str(probe.detail)


@pytest.mark.parametrize("field", ["error", "detail"])
def test_the_replacement_text_is_not_merely_truncated(field: str) -> None:
    """Truncation does not help — the leak is at the FRONT of an httpx message.

    Stated as a test so nobody 'fixes' a future case with ``str(exc)[:40]``,
    which would still begin with the host.
    """
    if field == "error":
        text = str(fetch_provider_auth(INTERNAL, transport=_refusing_transport())["error"])
    else:
        text = str(probe_observe(INTERNAL, transport=_refusing_transport()).detail)
    assert not text.startswith("[Errno"), f"looks like a truncated httpx message: {text!r}"
    _assert_no_leak(text)

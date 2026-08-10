"""Tests for the service adapters (#7-#9) using a mock HTTP transport."""

from __future__ import annotations

import httpx
import pytest

from cfactory.adapters import (
    AIFactoryAdapter,
    AdapterError,
    PFactoryAdapter,
    TFactoryAdapter,
    hydrate,
)
from cfactory.models import Service


def _transport(payload, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


def test_adapter_sends_bearer_token_when_configured():
    seen: dict[str, str | None] = {}

    def capture(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"tasks": []})

    adapter = AIFactoryAdapter("http://x", token="sekret", transport=httpx.MockTransport(capture))
    adapter.list_items()
    assert seen["auth"] == "Bearer sekret"


def test_adapter_sends_no_auth_header_without_token():
    seen: dict[str, str | None] = {}

    def capture(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"tasks": []})

    AIFactoryAdapter("http://x", transport=httpx.MockTransport(capture)).list_items()
    assert seen["auth"] is None


def test_pfactory_hits_plan_sessions_path_and_reads_sessions_envelope():
    seen: dict[str, str] = {}

    def capture(request):
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "sessions": [
                    {
                        "session_id": "s1",
                        "board_state": "human_review",
                        "github_issue": 42,
                        "title": "Login plan",
                    }
                ]
            },
        )

    items = PFactoryAdapter("http://x", transport=httpx.MockTransport(capture)).list_items()
    assert seen["path"] == "/api/plan/sessions"  # not the old /api/plans (404)
    assert len(items) == 1  # {"sessions": [...]} envelope is unwrapped
    assert items[0].correlation_key == "42"
    assert items[0].task_id == "s1"
    assert items[0].status == "human_review"


def test_probe_online_on_200():
    p = AIFactoryAdapter("http://x", transport=_transport({"tasks": []})).probe()
    assert p.online is True and p.status == "online"


@pytest.mark.parametrize("code", [401, 403])
def test_probe_unauthorized_on_401_403(code):
    # A reachable upstream that rejects us must NOT read as online.
    p = AIFactoryAdapter("http://x", transport=_transport({}, status=code)).probe()
    assert p.online is False and p.status == "unauthorized"
    assert "UPSTREAM_TOKEN" in (p.detail or "")


def test_probe_error_on_500():
    p = AIFactoryAdapter("http://x", transport=_transport({}, status=500)).probe()
    assert p.online is False and p.status == "error"


def test_probe_offline_on_connect_error():
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    p = AIFactoryAdapter("http://x", transport=httpx.MockTransport(boom)).probe()
    assert p.online is False and p.status == "offline"


def test_aifactory_normalizes_and_extracts_issue_key():
    payload = {
        "tasks": [
            {
                "id": "t1",
                "status": "coding",
                "phase": "code",
                "metadata": {"githubIssueNumber": 42},
                "title": "Login",
            }
        ]
    }
    adapter = AIFactoryAdapter("http://x", transport=_transport(payload))
    items = adapter.list_items()
    assert len(items) == 1
    it = items[0]
    assert it.correlation_key == "42"  # from metadata.githubIssueNumber
    assert it.service is Service.AIFACTORY
    assert it.task_id == "t1"
    assert it.status == "coding"
    assert it.phase == "code"


def test_pfactory_accepts_bare_list_and_board_state():
    payload = [
        {
            "session_id": "s1",
            "board_state": "human_review",
            "github_issue": 42,
            "title": "Login plan",
        }
    ]
    items = PFactoryAdapter("http://x", transport=_transport(payload)).list_items()
    assert items[0].correlation_key == "42"
    assert items[0].task_id == "s1"
    assert items[0].status == "human_review"
    assert items[0].phase == "plan"


def test_tfactory_reads_nested_provenance():
    payload = {
        "items": [
            {
                "spec_id": "spec1",
                "status": "triaged",
                "phase": "test",
                "source": {"aifactory": {"github_issue": 42}},
            }
        ]
    }
    items = TFactoryAdapter("http://x", transport=_transport(payload)).list_items()
    assert items[0].correlation_key == "42"
    assert items[0].task_id == "spec1"


def test_tfactory_lists_from_tfactory_tasks_endpoint():
    # The TEST stage lives under /api/tfactory/tasks, not the generic /api/tasks
    # (empty for verification runs). Guards the regression that left the cockpit's
    # TEST column permanently empty.
    assert TFactoryAdapter.list_path == "/api/tfactory/tasks"


def test_correlation_key_falls_back_to_task_id_when_no_issue():
    payload = {"tasks": [{"id": "t9", "status": "done"}]}
    items = AIFactoryAdapter("http://x", transport=_transport(payload)).list_items()
    assert items[0].correlation_key == "t9"


def test_http_error_raises_adapter_error():
    adapter = AIFactoryAdapter("http://x", transport=_transport({"e": 1}, status=500))
    with pytest.raises(AdapterError):
        adapter.list_items()


def test_hydrate_threads_services_by_correlation_key(store):
    pf = PFactoryAdapter(
        "http://x",
        transport=_transport(
            [{"session_id": "s1", "board_state": "done", "github_issue": 42, "title": "Login"}]
        ),
    )
    ai = AIFactoryAdapter(
        "http://x",
        transport=_transport(
            {"tasks": [{"id": "t1", "status": "coding", "metadata": {"githubIssueNumber": 42}}]}
        ),
    )

    n = hydrate(store, pf.list_items()) + hydrate(store, ai.list_items())
    assert n == 2

    wi = store.get("42")
    assert wi is not None
    assert wi.pfactory.status == "done"
    assert wi.aifactory.status == "coding"
    assert wi.title == "Login"  # set from the first item that carried one
    assert wi.timeline == []  # snapshots don't append events


def test_pfactory_adapter_carries_the_review_verdict():
    """The polled session row is how PFactory state actually reaches the cockpit (#245)."""
    row = {
        "session_id": "027-money-safe-vat-quote-endpoint",
        "board_state": "human_review",
        "gates_passed": False,
        "review": {
            "gates_passed": False,
            "threshold": 0.75,
            "aggregate_score": 0.94,
            "lenses": [{"lens": "security", "score": 0.70, "findings": [{"title": "No auth"}]}],
        },
    }
    item = PFactoryAdapter("http://x")._normalize(row)
    assert item is not None
    assert item.review["gates_passed"] is False
    # and it must survive the hop into the slice the cockpit reads
    assert item.to_state().extra["review"]["lenses"][0]["lens"] == "security"


def test_pfactory_adapter_synthesises_a_block_from_the_bare_boolean():
    """An older PFactory sends only `gates_passed`; that must still disable Approve."""
    item = PFactoryAdapter("http://x")._normalize(
        {"session_id": "s", "board_state": "human_review", "gates_passed": False}
    )
    assert item is not None
    assert item.review == {"gates_passed": False}


def test_pfactory_adapter_leaves_a_passing_or_unreviewed_session_alone():
    a = PFactoryAdapter("http://x")._normalize(
        {"session_id": "s", "board_state": "human_review", "gates_passed": True}
    )
    assert a is not None and a.review is None and a.to_state().extra == {}

    b = PFactoryAdapter("http://x")._normalize({"session_id": "s", "board_state": "draft"})
    assert b is not None and b.review is None


# --------------------------------------------------------------------------
# A refusal is not an outage (AIFactory#1126).
#
# AIFactory is translating handlers that returned {"success": false} inside an
# HTTP 200 into an honest 409 (#460). Without the branch these tests cover,
# raise_for_status turns every converted handler into a plain AdapterError,
# which the Services view renders as OFFLINE — swapping a failure disguised as
# success for a failure disguised as an outage, which sends the operator to the
# wrong system entirely.
# --------------------------------------------------------------------------


def test_409_is_a_refusal_not_a_generic_adapter_error():
    """The upstream ANSWERED. That must be distinguishable from unreachable."""
    from cfactory.adapters.base import AdapterRefusalError

    adapter = AIFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(409, json={"success": False, "error": "branch is dirty"})
        ),
    )
    try:
        adapter.list_items()
    except AdapterRefusalError as exc:
        assert exc.detail == "branch is dirty", exc.detail
        assert "refused (409)" in str(exc)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("409 did not raise AdapterRefusalError")


def test_refusal_is_still_an_adapter_error_so_existing_callers_do_not_break():
    """Subclass on purpose: the eight existing `except AdapterError` sites keep working."""
    from cfactory.adapters.base import AdapterError, AdapterRefusalError

    assert issubclass(AdapterRefusalError, AdapterError)


def test_a_real_outage_is_still_an_outage_not_a_refusal():
    """The other direction. A test that only proves 409 raises the new type would
    pass just as happily against code that raised it for everything."""
    from cfactory.adapters.base import AdapterError, AdapterRefusalError

    adapter = AIFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="upstream down")),
    )
    try:
        adapter.list_items()
    except AdapterRefusalError:  # pragma: no cover - this is the failure
        raise AssertionError("503 was misreported as a refusal")
    except AdapterError:
        pass
    else:  # pragma: no cover
        raise AssertionError("503 raised nothing")


def test_a_structured_upstream_error_is_coerced_to_str():
    """`.detail` is annotated `str | None`; an upstream may answer with a dict."""
    from cfactory.adapters.base import AdapterRefusalError

    adapter = AIFactoryAdapter(
        "http://x",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(409, json={"success": False, "error": {"code": 7}})
        ),
    )
    try:
        adapter.list_items()
    except AdapterRefusalError as exc:
        assert isinstance(exc.detail, str), type(exc.detail)
        assert "7" in exc.detail
    else:  # pragma: no cover
        raise AssertionError("409 did not raise")

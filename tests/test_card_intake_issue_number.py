"""A dispatched card carries its issue number, so usage correlates (#1418).

AIFactory keys the RFC-0001 completion event on the ISSUE NUMBER and falls back
to a synthetic `af-<spec_id>` when it has none. That fallback is correct for a
card with no issue, but a card imported FROM an issue that arrives without its
number degrades silently into an orphan: CFactory files the usage under
`af-<spec>` while the pollers key the same work by `<project>:<spec>` and
`<spec>`. Three rows for one piece of work, and the cost rollup reads the two
without usage.

Measured before the fix: every card dispatched from the planning board reported
`instrumented: false` and `$0.00`, while the usage itself was delivered
successfully and stored faithfully — against a key nothing reads.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from cfactory import card_intake
from cfactory.card_intake import _issue_number_payload


@pytest.mark.parametrize(
    ("issue_ref", "expected"),
    [
        ("olafkfreund/aifactory-demo#561", {"number": 561}),
        ("owner/repo#1", {"number": 1}),
        ("group/sub/project#42", {"number": 42}),  # GitLab-style nested path
    ],
)
def test_the_number_is_taken_from_the_issue_ref(issue_ref: str, expected: dict) -> None:
    assert _issue_number_payload(issue_ref) == expected


@pytest.mark.parametrize(
    "issue_ref",
    [None, "", "owner/repo", "owner/repo#", "owner/repo#abc", "#", "owner/repo#12x"],
)
def test_an_absent_or_unparseable_ref_sends_no_number(issue_ref: str | None) -> None:
    """An EMPTY mapping, not ``{"number": None}``.

    A card created on the board has no issue, and the synthetic fallback is the
    right behaviour for it. Sending an explicit null would look like a supplied
    value and could be written straight into `githubIssue.number`, which is the
    exact shape that produced this bug.
    """
    assert _issue_number_payload(issue_ref) == {}


def test_the_dispatch_payload_includes_the_number() -> None:
    """The wiring, not just the helper.

    A correct parser nobody splats into the payload changes nothing, and that is
    precisely the shape of #1418: every piece worked except the one that carried
    the value across the boundary.
    """
    src = inspect.getsource(card_intake)
    dispatch = src.split("AIFACTORY_INTAKE_ENDPOINT,", 1)[1]
    assert "_issue_number_payload(card.issue_ref)" in dispatch, (
        "the AIFactory dispatch payload no longer carries the issue number; "
        "completion events will orphan under af-<spec_id> again (#1418)"
    )

"""Issue identifiers are strings end to end (Factory#3774, RFC-0022 Phase 1)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from cfactory._contracts.factory_contracts import CorrelationBlock
from cfactory.git_providers import _issue_number_from_url
from cfactory.github_sync import IssueRef

_BACKEND = Path(__file__).parent.parent / "apps" / "backend" / "cfactory"


def test_issue_ref_holds_a_decimal_string_and_keeps_its_text_form() -> None:
    ref = IssueRef.parse("o/r#5")
    assert ref is not None
    assert ref.number == "5"
    assert str(ref) == "o/r#5"  # correlation/issue_ref text is byte-identical to the int era
    assert str(IssueRef("o/r", "5")) == "o/r#5"


def test_issue_ref_normalises_leading_zeros_like_the_int_era() -> None:
    ref = IssueRef.parse("o/r#007")
    assert ref is not None and str(ref) == "o/r#7"


@pytest.mark.parametrize("text", ["o/r#PROJ-123", "o/r#", "o/r#1.5"])
def test_a_non_numeric_key_is_rejected_at_the_edge(text: str) -> None:
    assert IssueRef.parse(text) is None


def test_issue_number_from_url_is_a_string() -> None:
    assert _issue_number_from_url("https://api.github.com/repos/o/r/issues/12") == "12"
    assert _issue_number_from_url("https://api.github.com/repos/o/r/issues/x") is None


def test_no_integer_comparison_against_an_issue_identifier() -> None:
    """Grep gate: `issue.number == 5` is False for "5", so it must not appear."""
    pattern = re.compile(r"\b(?:issue_number|issue_numbers|number|issue_ref)\b\s*[=!]=\s*\d")
    hits = [
        f"{path.name}:{n}"
        for path in _BACKEND.glob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, f"integer comparison against an issue identifier: {hits}"


def test_vendored_correlation_block_stringifies_a_legacy_integer() -> None:
    assert CorrelationBlock(issue_number=5).issue_number == "5"
    assert CorrelationBlock(issue_number="PROJ-1").issue_number == "PROJ-1"

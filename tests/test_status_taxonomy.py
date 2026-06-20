"""Tests for the canonical status taxonomy (#114).

The headline guarantee is token-exact matching: the previous substring-based
backend classification was buggy (``"ready"`` matched ``"already"``,
``"act"``/``"fail"`` matched any status merely containing those letters). These
tests pin the canonical helpers and prove the substring bug is gone.
"""

from __future__ import annotations

import pytest
from cfactory.status_taxonomy import (
    is_active,
    is_done,
    is_failed,
    is_failure_or_stuck,
    is_queued,
    is_review,
    is_running,
    is_stuck,
    is_terminal,
)

# --------------------------------------------------------------------------
# The substring bug: 'ready' must NOT match 'already' (and friends).
# --------------------------------------------------------------------------


def test_ready_token_no_longer_matches_already():
    # 'ready' is a done token; 'already' merely contains those letters.
    assert is_done("ready") is True
    assert is_done("already") is False
    assert is_terminal("already") is False
    # An active status that happens to contain 'already' stays active/running.
    assert is_active("already_running") is True
    assert is_running("already_running") is True


@pytest.mark.parametrize(
    ("status", "should_fail"),
    [
        ("failed", True),
        ("triager_failed", True),
        # 'fail' substring inside an unrelated word must NOT register as failure.
        ("failsafe_passed", False),
        ("unfailing", False),
    ],
)
def test_failure_is_token_exact(status, should_fail):
    assert is_failed(status) is should_fail


def test_compound_statuses_split_into_tokens():
    assert is_review("human_review") is True
    assert is_review("ai-review") is True
    assert is_done("qa_approved") is True
    assert is_queued("not_started") is False  # 'not'/'started' aren't queued tokens
    assert is_running("in_progress") is True


# --------------------------------------------------------------------------
# Classification basics.
# --------------------------------------------------------------------------


def test_terminal_covers_done_and_failed():
    assert is_terminal("done") is True
    assert is_terminal("merged") is True
    assert is_terminal("rejected") is True
    assert is_terminal("coding") is False
    assert is_terminal("human_review") is False
    assert is_terminal("queued") is False
    assert is_terminal(None) is False


def test_running_excludes_terminal_review_queued():
    assert is_running("coding") is True
    assert is_running("planning") is True
    assert is_running("done") is False
    assert is_running("human_review") is False
    assert is_running("backlog") is False
    # A present-but-unknown status reads as engaged/running; a falsy one does not.
    assert is_running("xyzzy") is True
    assert is_running(None) is False


def test_active_treats_missing_status_as_live():
    # Mirrors live-agent gating: a just-started agent has no mark yet.
    assert is_active(None) is True
    assert is_active("coding") is True
    for parked in ("human_review", "ai_review", "backlog", "pending", "queued", "todo"):
        assert is_active(parked) is False, parked
    for terminal in ("completed", "merged", "failed", "rejected", "cancelled"):
        assert is_active(terminal) is False, terminal


def test_stuck_and_failure_or_stuck():
    assert is_stuck("stuck") is True
    assert is_stuck("coding") is False
    # The anomaly detector treats both failures and explicit stuck as flaggable,
    # but the store's terminal check (is_failed) must NOT include 'stuck'.
    assert is_failure_or_stuck("stuck") is True
    assert is_failure_or_stuck("failed") is True
    assert is_failed("stuck") is False
    assert is_terminal("stuck") is False

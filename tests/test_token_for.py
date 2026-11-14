# ruff: noqa: S106  (fake tokens)
"""Settings.token_for: per-factory key wins, else the shared upstream token (#312)."""

from __future__ import annotations

import pytest
from cfactory.config import Settings


@pytest.mark.parametrize("svc", ["pfactory", "aifactory", "tfactory"])
def test_own_token_wins(svc):
    s = Settings(upstream_token="shared", **{f"{svc}_token": "own"})
    assert s.token_for(svc) == "own"


def test_falls_back_to_upstream():
    s = Settings(upstream_token="shared", pfactory_token="p")
    assert s.token_for("aifactory") == "shared"
    assert s.token_for("tfactory") == "shared"


def test_unknown_service_falls_back():
    assert Settings(upstream_token="shared", pfactory_token="p").token_for("nope") == "shared"


def test_empty_string_falls_back():
    assert Settings(upstream_token="shared", tfactory_token="").token_for("tfactory") == "shared"


def test_none_when_nothing_set():
    assert Settings(upstream_token=None).token_for("pfactory") is None

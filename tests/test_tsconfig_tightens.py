#!/usr/bin/env python3
"""The tighten-only check must catch a re-opened hole, not just pass on green.

Factory#546. A checker that only ever returns "ok" against the real tree is
indistinguishable from one that returns "ok" unconditionally, and the second
assertion here is the one with teeth: it feeds the checker the exact diff the
baseline forbids - `extends` present, one strict flag turned back off - and
requires a failure. That mutation is the whole reason the check exists, because
it is the shape that reads as conformant in review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_tsconfig_tightens import check  # noqa: E402

BASELINE = REPO_ROOT / "standards" / "tsconfig.base.json"
FRONTEND_TSCONFIG = REPO_ROOT / "apps" / "frontend-web" / "tsconfig.json"


def test_the_real_frontend_config_conforms() -> None:
    assert check(BASELINE, FRONTEND_TSCONFIG) == []


@pytest.mark.parametrize(
    "flag",
    [
        "noUncheckedIndexedAccess",
        "exactOptionalPropertyTypes",
        "noImplicitOverride",
        "noImplicitReturns",
        "verbatimModuleSyntax",
        "strict",
    ],
)
def test_re_opening_any_baseline_flag_fails(tmp_path: Path, flag: str) -> None:
    """The mutation: keep `extends`, turn one strict flag back off."""
    baseline = tmp_path / "tsconfig.base.json"
    baseline.write_text(BASELINE.read_text())

    config = json.loads(FRONTEND_TSCONFIG.read_text())
    config["extends"] = "./tsconfig.base.json"
    config["compilerOptions"][flag] = False
    child = tmp_path / "tsconfig.json"
    child.write_text(json.dumps(config))

    failures = check(baseline, child)
    assert failures, f"re-opening {flag} was not caught"
    assert any(flag in f for f in failures)


def test_dropping_extends_fails(tmp_path: Path) -> None:
    """A config with every flag inline but no `extends` inherits nothing."""
    baseline = tmp_path / "tsconfig.base.json"
    baseline.write_text(BASELINE.read_text())

    config = json.loads(FRONTEND_TSCONFIG.read_text())
    del config["extends"]
    child = tmp_path / "tsconfig.json"
    child.write_text(json.dumps(config))

    assert any("extends" in f for f in check(baseline, child))


def test_tightening_beyond_the_baseline_is_allowed(tmp_path: Path) -> None:
    """Turning ON something the baseline leaves off is the permitted direction."""
    baseline = tmp_path / "tsconfig.base.json"
    baseline.write_text(BASELINE.read_text())

    config = json.loads(FRONTEND_TSCONFIG.read_text())
    config["extends"] = "./tsconfig.base.json"
    config["compilerOptions"]["noPropertyAccessFromIndexSignature"] = True
    child = tmp_path / "tsconfig.json"
    child.write_text(json.dumps(config))

    assert check(baseline, child) == []

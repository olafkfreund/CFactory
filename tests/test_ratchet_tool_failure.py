#!/usr/bin/env python3
"""A linter that never ran must fail the ratchet, not read as "no violations".

PFactory#455 (ruff), same shape found and fixed in TFactory#951. Swept across
the fleet because two repos finding it independently makes it a class: a
subprocess whose stdout is read for results while its returncode is ignored.

``ruff check`` exits 0 clean, 1 with violations, and >=2 on its OWN failure -
binary missing, config parse error, bad argv - writing nothing to stdout. A
CLEAN run prints ``[]``, never nothing. So empty stdout was never the clean
case, and treating it as one let both sides of the base-vs-head comparison come
back 0 and the gate report "no regression" having measured nothing.

``mypy`` has the same three-way exit code with one wrinkle: it also exits 2 on a
BLOCKING error (a syntax error in the file under test). That case still emits an
error line, so it is counted and gated normally; only a failed run that produced
no error line at all is treated as "did not run".

"An error line" means one naming THE FILE UNDER TEST, since CFactory#319 -- a
blocking error in a file the target merely imports names that other file, and
counting it gated a clean file at 1. So the mypy stubs here name the path the
ratchet handed mypy rather than an arbitrary one.

The controls are the assertions with teeth in the other direction. A guard that
fired on every non-zero exit would break the ordinary "violations found" path
(exit 1), and for mypy it would abort the ratchet on a syntax error rather than
blocking it as the regression it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# scripts/ is put on sys.path by tests/conftest.py.
import ratchet_lint as rl

_FILE = "apps/backend/cfactory/main.py"


@pytest.fixture(autouse=True)
def _at_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # mypy_count() materialises its temp copy NEXT TO the file, so the path has
    # to resolve - and the ratchet always runs from the repo root, as CI invokes
    # it. Without this the fixture-local cwd would decide the verdict.
    monkeypatch.chdir(Path(__file__).resolve().parents[1])


class _Res:
    """The subset of CompletedProcess the ratchet reads."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    def _apply(res: _Res) -> None:
        monkeypatch.setattr(rl, "_run", lambda *_a, **_k: res)

    return _apply


@pytest.fixture
def stub_mypy(monkeypatch: pytest.MonkeyPatch):
    """Stub mypy so its error lines name the file the ratchet ACTUALLY handed it.

    The counter compares that path now (CFactory#319), so a stub naming some
    other file no longer stands in for "mypy reported on our file" -- which is
    the whole point of the fix. Reading the target out of argv keeps these stubs
    honest without hard-coding the temp name, which is random per call.
    """

    def _apply(returncode: int, *messages: str) -> None:
        def run(cmd: list[str], **_kwargs: object) -> _Res:
            target = cmd[-1]
            return _Res(
                returncode,
                stdout="".join(f"{target}:{i + 1}: error: {m}\n" for i, m in enumerate(messages)),
            )

        monkeypatch.setattr(rl, "_run", run)

    return _apply


# --------------------------------------------------------------------------- #
# ruff                                                                         #
# --------------------------------------------------------------------------- #


def test_ruff_own_failure_exits_rather_than_reporting_clean(stub) -> None:
    stub(_Res(2, stderr="error: invalid value for '--config <CONFIG_OPTION>'"))
    with pytest.raises(SystemExit) as exc:
        rl.ruff_counts("x = 1\n", _FILE)
    assert exc.value.code == 2


def test_ruff_failure_surfaces_stderr_for_diagnosis(stub, capsys) -> None:
    stub(_Res(2, stderr="does not point to a configuration file"))
    with pytest.raises(SystemExit):
        rl.ruff_counts("x = 1\n", _FILE)
    assert "does not point to a configuration file" in capsys.readouterr().err


def test_ruff_clean_file_still_counts_zero(stub) -> None:
    # Control: exit 0 with "[]" is ruff saying "checked it, nothing wrong".
    stub(_Res(0, stdout="[]"))
    assert rl.ruff_counts("x = 1\n", _FILE) == {}


def test_ruff_violations_are_still_counted(stub) -> None:
    # Control: exit 1 is the ordinary "found something" path, not a failure.
    stub(_Res(1, stdout='[{"code": "S101"}, {"code": "S101"}]'))
    assert rl.ruff_counts("x = 1\n", _FILE)["S101"] == 2


def test_ruff_writing_nothing_at_all_exits_rather_than_counting_zero(stub) -> None:
    """Factory#648: empty stdout was never the clean case.

    A clean run prints `[]`. Empty stdout on an exit-0 run is ruff having
    written no report, and the `return Counter()` that used to sit here counted
    it as perfection -- the same nothing-reads-as-clean defect Factory#590
    closed one exit code over, which `require_tool_ran` cannot reach because the
    process exited 0.

    This is the WIRING proof: that this fork routes its parse through
    `ratchet_helpers.ruff_findings` rather than restating it. No byte comparison
    can see a restatement, which is why the rule is also registered in the hub
    gate's _REQUIRED_RATCHET_RULES.
    """
    stub(_Res(0, stdout="   \n"))
    with pytest.raises(SystemExit) as exc:
        rl.ruff_counts("x = 1\n", _FILE)
    assert exc.value.code == 2


def test_ruff_output_that_is_not_json_exits_rather_than_counting_zero(stub, capsys) -> None:
    # Factory#648: with `fix = true` reachable in a config ruff writes the FIXED
    # SOURCE to stdout and exits 0, so the parse would read Python as findings.
    # The canonical now says so; this used to be a bare `except` with no message.
    stub(_Res(0, stdout="import os\n\nx = 1\n"))
    with pytest.raises(SystemExit) as exc:
        rl.ruff_counts("x = 1\n", _FILE)
    assert exc.value.code == 2
    assert "not the JSON finding list" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# mypy                                                                         #
# --------------------------------------------------------------------------- #


def test_mypy_own_failure_exits_rather_than_reporting_zero_errors(stub) -> None:
    stub(_Res(2, stderr="mypy: error: Cannot find config file 'standards/mypy.ini'"))
    with pytest.raises(SystemExit) as exc:
        rl.mypy_count("x = 1\n", _FILE)
    assert exc.value.code == 2


def test_mypy_failure_surfaces_stderr_for_diagnosis(stub, capsys) -> None:
    stub(_Res(2, stderr="mypy: error: unrecognized arguments: --bogus"))
    with pytest.raises(SystemExit):
        rl.mypy_count("x = 1\n", _FILE)
    assert "unrecognized arguments" in capsys.readouterr().err


def test_mypy_clean_file_still_counts_zero(stub) -> None:
    # Control: exit 0 with no error lines is a genuinely clean file.
    stub(_Res(0))
    assert rl.mypy_count("x = 1\n", _FILE) == 0


def test_mypy_blocking_error_is_counted_not_treated_as_a_crash(stub_mypy) -> None:
    # Control with teeth: a syntax error exits 2 as well, but still emits an
    # error line. Keying the guard on the exit code alone would abort the
    # ratchet here instead of blocking the regression.
    stub_mypy(2, "invalid syntax  [syntax]")
    assert rl.mypy_count("x = 1\n", _FILE) == 1


def test_mypy_errors_are_still_counted(stub_mypy) -> None:
    # Control: exit 1 is the ordinary "found something" path.
    stub_mypy(
        1,
        "Function is missing a type annotation  [no-untyped-def]",
        "Returning Any from function  [no-any-return]",
    )
    assert rl.mypy_count("x = 1\n", _FILE) == 2

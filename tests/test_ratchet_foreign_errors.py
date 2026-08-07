#!/usr/bin/env python3
"""The ratchet counts errors mypy blamed on THIS file, not on some other one.

CFactory#319 (and Factory#601, the identical hub fork). ``mypy_count`` matched
every ``path:line: error:`` line in mypy's output and counted it, while the regex
did not even capture the path -- so nothing could have compared it. PFactory,
TFactory and AIFactory all compare; this fork and the hub were the two outliers.

WHY THAT IS REACHABLE, given ``--follow-imports=silent``. Silent covers ordinary
errors in imported modules. It does not cover a BLOCKING one: an import that
fails to parse prints its own error line and mypy stops there, before the target
is type-checked at all. The ratchet then attributed that foreign line to the file
under test -- a clean file came back as 1 -- and, worse, base and HEAD can blame
a different set of foreign files, so the comparison could report a regression
that is not one or hide one that is.

THE COMPARISON IS RESOLVED ON BOTH SIDES, which is this fork's own wrinkle. The
temp copy lives INSIDE the tree (``materialized`` writes it next to the original
so relative imports resolve, issue #193), so mypy prints it relative to the
working directory while the ratchet holds an absolute path. A string compare
would match nothing and count zero for every file -- the same gate failure
pointing the other way, which is why the second test here exists.

These run REAL mypy against a real broken import, because the defect only exists
in the shape of mypy's actual output.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

# scripts/ is put on sys.path by tests/conftest.py.
import ratchet_lint as rl

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _REPO_ROOT / "apps" / "backend" / "cfactory"
_UNDER_TEST = "apps/backend/cfactory/_ratchet_probe.py"


@pytest.fixture(autouse=True)
def _at_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # mypy_count materialises its temp copy NEXT TO the file, and the ratchet
    # always runs from the repo root as CI invokes it.
    monkeypatch.chdir(_REPO_ROOT)


@pytest.fixture
def broken_import() -> Iterator[str]:
    """A module in the package that does not parse, and the import line for it."""
    module = _PACKAGE / "_ratchet_probe_broken.py"
    module.write_text("def oops(:\n    pass\n")
    try:
        yield "from cfactory._ratchet_probe_broken import oops\n\n\ndef go() -> None:\n    oops()\n"
    finally:
        module.unlink(missing_ok=True)


def test_a_foreign_files_blocking_error_is_not_counted_as_this_files(
    broken_import: str,
) -> None:
    """The case from the issue: the target is clean, its import will not parse."""
    # Pre-fix this returned 1, silently, for a file with nothing wrong with it.
    # mypy never reached the target, so the honest verdict is "could not
    # measure" (exit 2) -- not 1, and not a fabricated 0 either.
    with pytest.raises(SystemExit) as exc:
        rl.mypy_count(broken_import, _UNDER_TEST)
    assert exc.value.code == 2


def test_the_files_own_errors_are_still_counted() -> None:
    """Teeth the other way, and the reason the comparison is resolved.

    mypy names the temp copy relative to the working directory; the ratchet holds
    it absolute. Comparing those as strings counts zero for every file and turns
    the whole mypy ratchet into a no-op that passes.
    """
    assert rl.mypy_count('def go() -> int:\n    return "not an int"\n', _UNDER_TEST) == 1


def test_a_blocking_error_in_the_file_under_test_is_still_counted() -> None:
    """mypy exits 2 here too, but the error IS this file's, so it is a regression."""
    assert rl.mypy_count("def go(:\n    pass\n", _UNDER_TEST) == 1

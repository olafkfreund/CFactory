#!/usr/bin/env python3
"""The lint ratchet applies the test assert bar to tests, and only to tests.

Factory#510. CFactory's ratchet linted a temp COPY of each changed file, and
ruff relativises a path against the project root before matching
per-file-ignores — so a path outside that root falls back to matching the
BASENAME only. Two of ruff.toml's three carve-outs therefore worked under the
ratchet (``**/test_*.py``, ``**/*_test.py``) and one could never match at all
(``**/tests/**``). A helper under ``tests/`` named neither way — this repo ships
``tests/cards_harness.py`` — was held to the production assert bar by the gate
while ``ruff check`` on the real tree exempted it. Two tools disagreeing about
what a test is, which is the mismatch the shared ``is_test_file`` was extracted
to prevent (Factory#403).

The fix is not a better temp path: mirroring the directories inside the temp dir
(``<tmpdir>/tests/helpers.py``) still reports S101, measured. Ruff is told the
file's REAL repo-relative path via ``--stdin-filename`` and gets the source on
stdin, so there is no copy to misjudge.

Both verdicts are asserted, and the second is the one with teeth: exempting
unconditionally — or losing the path in a way that made everything look like a
test — passes the first and silently drops S101 for the whole repo.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import ratchet_helpers as rh

# scripts/ is put on sys.path by tests/conftest.py.
import ratchet_lint as rl

_ASSERTION = "assert 1 == 2\n"


@pytest.fixture(autouse=True)
def _at_repo_root() -> Iterator[None]:
    """Run these cases from the repo root, because the ratchet always is.

    ``ruff_counts`` passes ``--config standards/ruff.toml`` and a repo-relative
    ``--stdin-filename``, and BOTH resolve against the current directory. CI
    invokes the ratchet from the root so that is correct there — but pytest run
    from ``apps/backend`` reads a different config and turns
    ``apps/backend/...`` into ``apps/backend/apps/backend/...``. That silently
    emptied the verdict and the assertion with teeth went green-on-nothing.
    """
    original = Path.cwd()
    os.chdir(Path(__file__).resolve().parents[1])
    try:
        yield
    finally:
        os.chdir(original)


def test_ruff_exempts_every_shape_of_test_path() -> None:
    named = rl.ruff_counts(_ASSERTION, "tests/test_x.py")
    helper = rl.ruff_counts(_ASSERTION, "tests/cards_harness.py")
    assert "S101" not in helper, "a helper under tests/ must get the test assert carve-out"
    assert helper == named, "two files under tests/ must not get two different verdicts"


def test_ruff_still_holds_production_to_the_assert_bar() -> None:
    # THE ASSERTION WITH TEETH.
    assert "S101" in rl.ruff_counts(_ASSERTION, "apps/backend/cfactory/prod.py")


def test_the_ruff_rule_lives_in_the_canonical_module() -> None:
    """The ratchet must CONSUME the shared rule, not carry its own copy.

    Factory#403: the fork that used to live here (``ruff_materialized``) is why
    this repo needed its own fix for a bug the hub had already fixed once.
    """
    for name in ("is_test_file", "ruff_stdin_argv", "MYPY_TEST_RELAX"):
        assert getattr(rl, name) is getattr(rh, name), f"{name} is not the canonical object"

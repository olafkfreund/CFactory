#!/usr/bin/env python3
"""Diff-scoped lint ratchet for the CFactory Python backend.

Implements the Factory coding-standards ratchet (coding-standards.md sections 0
and 4.6): the strict bar (`ruff` with the shared select set + `mypy --strict`)
is enforced on the files a PR changes, and a changed file MAY NOT REGRESS - i.e.
it may not gain ruff or mypy violations relative to the PR base. Untouched
legacy hotspots are allowed until touched, and the existing legacy backlog
inside a touched file does not block (a whole-repo strict gate would be
instantly red: 127 legacy violations at adoption). New code and any net-new
violation a PR introduces are blocked.

Mechanism: for each changed Python file, count violations (ruff: per rule code;
mypy: per file) at the PR base and at HEAD; fail if HEAD has more. `ruff format`
reflowing legacy lines never increases the count, so a pure-cleanup PR stays
green while genuine new violations are caught.

Two tools are supported (mirrors AIFactory scripts/cq_ratchet.py):

* ``--tool ruff`` - per-rule-code ruff violation counts on each changed file.
* ``--tool mypy`` - mypy --strict error count per changed file. The legacy tree
  is only partially annotated, so a whole-tree strict run would be instantly
  red; counting per file base-vs-head lets a touched legacy file keep its
  existing mypy debt while forbidding NET-NEW type errors.

Usage:
    python scripts/ratchet_lint.py --base <git-ref> [--tool ruff|mypy] [--package <dir>]

Exit code 0 if no changed file regressed; 1 otherwise.
"""

# T201 (no print) targets SERVICE code, where stdout is not an output channel.
# This is a CI command-line tool whose entire product is what it prints: the
# gated file list, the per-rule regression lines and the verdict all go to the
# CI log, and the workflow has no other way to show them. Scoped to the one
# rule: the code-less blanket form is what PGH004 forbids, and it would hide the
# next real finding in this file. Same carve-out PFactory's fork already carries.
# ruff: noqa: T201

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Canonical shared ratchet rules, vendored byte-exact from the Factory hub
# and byte-exact drift-gated (Factory#403). scripts/ is sys.path[0] when this
# runs as a script, so the sibling import resolves without packaging.
from ratchet_helpers import (
    MYPY_TEST_RELAX,
    is_test_file,
    require_tool_ran,
    ruff_findings,
    ruff_stdin_argv,
)

# The VENDORED baseline, not the repo-wide config at the root (Factory#513).
# The ratchet holds new and touched code to the full shared bar; the root
# ruff.toml is this repo's own config, which extends the same baseline today but
# is the file that would carry a documented carve-out. Pointing the ratchet at
# the root would let such a carve-out quietly lower the bar the ratchet exists
# to hold — the same shape as gating against a stale canonical.
RUFF_CONFIG = "standards/ruff.toml"
MYPY_CONFIG = "standards/mypy.ini"

PACKAGE_DEFAULT = "apps/backend/cfactory"

# Byte-exact vendored copies of Factory-hub canonicals. These are NOT governed by
# this repo's strict bar: they must stay identical to the hub and are policed by
# the verification-core drift gate instead, so "fixing" one here to satisfy the
# ratchet is what breaks the next re-vendor (Factory#403). ruff.toml excludes the
# same path for the format gate; this ratchet reads standards/ruff.toml directly
# and so cannot see that exclusion. Paths are repo-relative.
VENDORED_SKIP = frozenset({"scripts/ratchet_helpers.py"})

# mypy text output lines look like:  path/to/file.py:12: error: <msg>  [code]
# The path is CAPTURED because it has to be compared: see mypy_count.
_MYPY_ERROR_RE = re.compile(r"^(?P<path>.+?):\d+: error:")


def _run(
    cmd: list[str], env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    # S603: every argv reaching here is assembled in this file from repo-relative
    # config paths and `git`/`ruff`/`mypy` literals — no shell, and no caller-
    # supplied string ever becomes a command word. This is a CI developer tool,
    # not a request-handling surface.
    return subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=False, env=env, input=stdin
    )


def changed_python_files(base: str, packages: list[str]) -> list[str]:
    """Python files under any of *packages* changed (added/modified) vs *base*."""
    res = _run(["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD"])
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        sys.exit(2)
    pkgs = [Path(p) for p in packages]
    out: list[str] = []
    for line in res.stdout.splitlines():
        path = Path(line)
        if line in VENDORED_SKIP:
            continue
        if path.suffix == ".py" and any(p in path.parents for p in pkgs) and path.exists():
            out.append(str(path))
    return out


@contextmanager
def materialized(source: str, filename: str) -> Iterator[str]:
    """Write *source* to a unique temp file NEXT TO *filename*, yielding its path.

    The copy must live inside the package directory: from a /tmp path the
    file's own relative imports (`from . import __version__`) cannot resolve,
    so mypy reports "No parent module -- cannot perform relative import" and
    then degrades e.g. `fastapi.APIRouter` to `Any`, inventing an
    `untyped-decorator` error that does not exist in place (issue #193). Both
    the base and the HEAD source go through here, so the comparison stays
    symmetric.
    """
    target = Path(filename)
    with tempfile.NamedTemporaryFile(
        "w", dir=target.parent, prefix="_ratchet_", suffix=f"__{target.name}", delete=False
    ) as fh:
        fh.write(source)
        tmp = fh.name
    try:
        yield tmp
    finally:
        Path(tmp).unlink(missing_ok=True)


def ruff_counts(source: str, filename: str) -> Counter[str]:
    """Per-rule ruff violation counts for *source* checked as *filename*.

    Fed on stdin under the file's REAL path so ruff's per-file-ignores see the
    same path ``ruff check`` would (Factory#510). The temp copy this used to
    write could not: ruff relativises a path against the project root before
    matching the globs, and a path OUTSIDE that root falls back to the BASENAME
    only. ``**/test_*.py`` and ``**/*_test.py`` therefore matched a copy but
    ``**/tests/**`` never could, so a helper under ``tests/`` named neither way
    (``tests/cards_harness.py`` here) was held to the production assert bar the
    real tree exempts it from.
    """
    res = _run(ruff_stdin_argv(RUFF_CONFIG, filename), stdin=source)
    # The shared "is this run a measurement" rule, both halves (Factory#590 for
    # the exit code, Factory#648 for the output). This used to be an exit-code
    # check plus a `return Counter()` for empty stdout plus a bare
    # `except json.JSONDecodeError`, restated here and in the four sibling
    # ratchets. The empty-stdout branch was the one with teeth: the pinned ruff
    # prints `[]` for a clean run -- including for empty stdin -- so empty
    # stdout was always ruff writing no report, counted as zero violations.
    # Both verdicts now live in the drift-gated canonical.
    return ruff_findings(res)


def mypy_command(target: str, original: str | None = None) -> list[str]:
    """The mypy invocation used for both the base and HEAD version of a file.

    ``--follow-imports=silent`` keeps mypy from reporting errors in imported
    legacy modules the changed file merely references, and
    ``--ignore-missing-imports`` stops third-party stub gaps (and the base
    version's temp-file location) from inflating the count - the strict bar
    still applies to the file's own annotations.
    """
    return [
        "mypy",
        "--config-file",
        MYPY_CONFIG,
        "--ignore-missing-imports",
        "--follow-imports=silent",
        "--no-error-summary",
        "--no-color-output",
        "--hide-error-context",
        *(MYPY_TEST_RELAX if is_test_file(original if original is not None else target) else []),
        target,
    ]


def mypy_count(source: str, filename: str) -> int:
    """mypy --strict error count for *source* checked as *filename*.

    Only lines mypy attributed to the file it was HANDED are counted, and that is
    the temp copy, not the repo-relative original. ``--follow-imports=silent``
    silences imported modules for ordinary errors but NOT for a blocking one: an
    import that fails to parse prints its own error line and stops the run before
    the target is checked at all. Counting that line attributed a foreign file's
    error to this one — measured, a clean file whose import would not parse came
    back as 1 (CFactory#319, and Factory#601 for the identical hub fork). PFactory,
    TFactory and AIFactory already compared the path; this fork did not, and its
    regex did not even capture it.

    The comparison is RESOLVED on both sides. mypy prints the temp copy relative
    to the working directory — the copy lives inside the tree, unlike the hub's,
    whose /tmp path comes back verbatim — while ``materialized`` yields an
    absolute one. Comparing the strings would match nothing and count zero for
    every file, which is the same gate failure pointing the other way.

    A zero count out of a blocking run is not "clean" either, and is not treated
    as one: ``require_tool_ran`` sees exit 2 with nothing attributed to the target
    and aborts with "could not measure", which is the truthful verdict when mypy
    never reached the file.

    Base and HEAD are both checked from a temp copy next to the original (see
    `materialized`) so the comparison is symmetric and relative imports resolve.
    That copy carries a random name, so two runs never share a mypy cache entry
    and a cache hit cannot replay a stale path — measured, each run is blamed on
    its own filename.
    """
    with materialized(source, filename) as tmp:
        res = _run(mypy_command(tmp, filename), env={**os.environ, "MYPYPATH": "apps/backend"})
        target = Path(tmp).resolve()
        count = sum(
            1
            for line in res.stdout.splitlines()
            if (m := _MYPY_ERROR_RE.match(line)) is not None
            and Path(m.group("path")).resolve() == target
        )
        # Same shared rule as the ruff counter, with `measured` passed: mypy's exit 2
        # also covers a BLOCKING error, which still names a file and so belongs in the
        # count rather than aborting the run.
        require_tool_ran("mypy", res, measured=count)
        return count


def file_at_base(base: str, path: str) -> str | None:
    res = _run(["git", "show", f"{base}:{path}"])
    return res.stdout if res.returncode == 0 else None


def regressions(base: str, path: str, tool: str) -> list[str]:
    head_src = Path(path).read_text()
    base_src = file_at_base(base, path)
    if tool == "mypy":
        head_n = mypy_count(head_src, path)
        base_n = mypy_count(base_src, path) if base_src is not None else 0
        if head_n > base_n:
            return [f"{path}: mypy errors +{head_n - base_n} (base {base_n} -> head {head_n})"]
        return []
    head_counts = ruff_counts(head_src, path)
    base_counts = ruff_counts(base_src, path) if base_src is not None else Counter()
    out: list[str] = []
    for code, head_n in head_counts.items():
        base_n = base_counts.get(code, 0)
        if head_n > base_n:
            out.append(f"{path}: {code} +{head_n - base_n} (base {base_n} -> head {head_n})")
    return out


# --self-test probes. The clean one uses the house relative-import style that
# used to be reported as "No parent module" from a /tmp copy (issue #193); the
# broken one carries exactly one genuine strict-mode error.
_PROBE_CLEAN = '''"""Self-test probe for the ratchet."""

from __future__ import annotations

from . import __version__


def probe() -> str:
    return __version__
'''
_PROBE_BROKEN = _PROBE_CLEAN.replace("-> str:", "-> int:")


def self_test(package: str) -> int:
    """Check the ratchet's own legs against synthetic probes in *package*."""
    probe = str(Path(package) / "ratchet_self_test_probe.py")
    clean_n = mypy_count(_PROBE_CLEAN, probe)
    broken_n = mypy_count(_PROBE_BROKEN, probe)
    checks = [
        ("mypy: relative-import probe is clean (issue #193)", clean_n == 0, f"got {clean_n}"),
        ("mypy: genuine type error is caught", broken_n > clean_n, f"got {broken_n}"),
        ("ruff: unused import is flagged", ruff_counts("import os\n", probe)["F401"] == 1, ""),
    ]
    failed = 0
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' ({detail})' if not ok and detail else ''}")
        failed += not ok
    print("self-test PASSED" if not failed else f"self-test FAILED ({failed} check(s))")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="git ref to diff against")
    parser.add_argument("--tool", choices=["ruff", "mypy"], default="ruff")
    # Repeatable (Factory#597), matching PFactory's and TFactory's forks of this
    # file: the ratchet has to reach scripts/ as well as the backend package, and
    # the alternative — invoking the whole ratchet twice from the workflow —
    # doubles the run and prints two verdicts for one question.
    parser.add_argument("--package", action="append", dest="packages", default=None)
    parser.add_argument("--self-test", action="store_true", help="check the ratchet itself")
    args = parser.parse_args()
    packages = args.packages or [PACKAGE_DEFAULT]

    if args.self_test:
        # The probes are relative-import ones, so they only make sense inside the
        # backend package; the first --package is that package by convention.
        return self_test(packages[0])
    if not args.base:
        parser.error("--base is required (unless --self-test)")

    files = changed_python_files(args.base, packages)
    if not files:
        joined = ", ".join(packages)
        print(f"ratchet ({args.tool}): no changed Python files in {joined}; nothing to gate.")
        return 0

    print(f"ratchet ({args.tool}): gating changed files:\n  " + "\n  ".join(files))

    all_regressions: list[str] = []
    regressed_paths: list[str] = []
    for path in files:
        found = regressions(args.base, path, args.tool)
        all_regressions.extend(found)
        if found:
            regressed_paths.append(path)

    if all_regressions:
        print(f"\nratchet FAILED: changed files gained {args.tool} violations (shared strict bar):")
        for line in all_regressions:
            print(f"  {line}")
        if args.tool == "mypy":
            # Show the actual findings to make the failure actionable.
            for path in regressed_paths:
                res = _run(mypy_command(path), env={**os.environ, "MYPYPATH": "apps/backend"})
                sys.stdout.write(res.stdout)
        print(
            "\nFix the new violations (or clean the file further). The ratchet only "
            "blocks NET-NEW violations - pre-existing legacy in a touched file is "
            "allowed (coding-standards.md section 4.6)."
        )
        return 1

    print("ratchet PASSED: no changed file regressed; new violations: none.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

from __future__ import annotations

import argparse
import json
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
from ratchet_helpers import MYPY_TEST_RELAX, is_test_file, ruff_stdin_argv

# The VENDORED baseline, not the repo-wide config at the root (Factory#513).
# The ratchet holds new and touched code to the full shared bar; the root
# ruff.toml is this repo's own config, which extends the same baseline today but
# is the file that would carry a documented carve-out. Pointing the ratchet at
# the root would let such a carve-out quietly lower the bar the ratchet exists
# to hold — the same shape as gating against a stale canonical.
RUFF_CONFIG = "standards/ruff.toml"
MYPY_CONFIG = "standards/mypy.ini"

PACKAGE_DEFAULT = "apps/backend/cfactory"

# mypy text output lines look like:  path/to/file.py:12: error: <msg>  [code]
_MYPY_ERROR_RE = re.compile(r"^.+?:\d+: error:")


def _run(
    cmd: list[str], env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env, input=stdin)


def changed_python_files(base: str, package: str) -> list[str]:
    """Python files under *package* changed (added/modified) vs *base*."""
    res = _run(["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD"])
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        sys.exit(2)
    pkg = Path(package)
    out: list[str] = []
    for line in res.stdout.splitlines():
        path = Path(line)
        if path.suffix == ".py" and pkg in path.parents and path.exists():
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
    # ruff exits 0 clean, 1 with violations, and >=2 on its OWN failure: binary
    # missing, config parse error, bad argv. Those write nothing to stdout, so
    # without this the empty-stdout branch below reads "ruff is broken" as "no
    # violations" and the ratchet passes green on an unrunnable linter. A clean
    # run prints "[]", never nothing (PFactory#455, TFactory#951).
    if res.returncode not in (0, 1):
        sys.stderr.write(res.stdout + res.stderr)
        sys.exit(2)
    if not res.stdout.strip():
        return Counter()
    try:
        items = json.loads(res.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(res.stdout + res.stderr)
        sys.exit(2)
    return Counter(item["code"] for item in items)


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

    With ``--follow-imports=silent`` mypy only reports errors in the file it
    was explicitly given, so every error line belongs to this file. Base and
    HEAD are both checked from a temp copy next to the original (see
    `materialized`) so the comparison is symmetric and relative imports resolve.
    """
    with materialized(source, filename) as tmp:
        res = _run(mypy_command(tmp, filename), env={**os.environ, "MYPYPATH": "apps/backend"})
        count = sum(1 for line in res.stdout.splitlines() if _MYPY_ERROR_RE.match(line))
        # Same defect as the ruff branch above, in the other half of the gate.
        # mypy exits 0 clean, 1 with errors, and 2 both for its OWN failure
        # (missing config, bad argv) and for a blocking error. A blocking error
        # still emits an error LINE, so it lands in `count`; mypy failing to run
        # emits none. Zero errors out of a failed run is "did not run", not
        # "clean", and returning it makes the base-vs-head comparison pass green
        # having measured nothing (PFactory#455, TFactory#951).
        if res.returncode not in (0, 1) and count == 0:
            sys.stderr.write(res.stdout + res.stderr)
            sys.exit(2)
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
    parser.add_argument("--package", default=PACKAGE_DEFAULT)
    parser.add_argument("--self-test", action="store_true", help="check the ratchet itself")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.package)
    if not args.base:
        parser.error("--base is required (unless --self-test)")

    files = changed_python_files(args.base, args.package)
    if not files:
        print(f"ratchet ({args.tool}): no changed Python files in {args.package}; nothing to gate.")
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

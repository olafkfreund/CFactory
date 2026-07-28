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
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PACKAGE_DEFAULT = "apps/backend/cfactory"

# mypy text output lines look like:  path/to/file.py:12: error: <msg>  [code]
_MYPY_ERROR_RE = re.compile(r"^.+?:\d+: error:")


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)


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


@contextmanager
def ruff_materialized(source: str, filename: str) -> Iterator[str]:
    """Materialise *source* for RUFF under its REAL basename in a fresh temp dir.

    Deliberately NOT ``materialized()`` above. That one writes beside the target
    because mypy needs the real package location or relative imports break
    (issue #193) - but it uses a random prefix, which defeats ruff
    per-file-ignores like ``**/test_*.py``. A net-new test file was therefore
    held to the non-test bar and tripped S101 while clean under its real path.

    The two tools want different things and cannot share one strategy: mypy
    needs the LOCATION, ruff needs the BASENAME. ruff resolves no imports, so a
    fresh directory costs it nothing. Factory#403.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        tmp = Path(tmpdir) / Path(filename).name
        tmp.write_text(source)
        yield str(tmp)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ruff_counts(source: str, filename: str) -> Counter[str]:
    """Per-rule ruff violation counts for *source* checked as *filename*."""
    with ruff_materialized(source, filename) as tmp:
        res = _run(["ruff", "check", "--config", "ruff.toml", "--output-format", "json", tmp])
        if not res.stdout.strip():
            return Counter()
        try:
            items = json.loads(res.stdout)
        except json.JSONDecodeError:
            sys.stderr.write(res.stdout + res.stderr)
            sys.exit(2)
        return Counter(item["code"] for item in items)


def _is_test_file(path: str) -> bool:
    """Does *path* name a test file, by the same shape ruff per-file-ignores use?

    Kept deliberately in step with the ruff config (`**/test_*.py`,
    `**/*_test.py`, `**/tests/**`) so one tool cannot treat a file as a test
    while the other holds it to the production bar. Ported from the hub
    canonical (Factory#403).
    """
    norm = path.replace("\\", "/")
    name = norm.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{norm}"
        or "/test/" in f"/{norm}"
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


_MYPY_TEST_RELAX = ["--allow-untyped-defs", "--allow-incomplete-defs"]


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
        "mypy.ini",
        "--ignore-missing-imports",
        "--follow-imports=silent",
        "--no-error-summary",
        "--no-color-output",
        "--hide-error-context",
        *(_MYPY_TEST_RELAX if _is_test_file(original if original is not None else target) else []),
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
        return sum(1 for line in res.stdout.splitlines() if _MYPY_ERROR_RE.match(line))


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

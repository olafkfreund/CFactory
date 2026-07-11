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


def ruff_counts(source: str, filename: str) -> Counter[str]:
    """Per-rule ruff violation counts for *source* checked as *filename*."""
    suffix = Path(filename).name
    with tempfile.NamedTemporaryFile("w", suffix=f"__{suffix}", delete=False) as fh:
        fh.write(source)
        tmp = fh.name
    try:
        res = _run(["ruff", "check", "--config", "ruff.toml", "--output-format", "json", tmp])
        if not res.stdout.strip():
            return Counter()
        try:
            items = json.loads(res.stdout)
        except json.JSONDecodeError:
            sys.stderr.write(res.stdout + res.stderr)
            sys.exit(2)
        return Counter(item["code"] for item in items)
    finally:
        Path(tmp).unlink(missing_ok=True)


def mypy_command(target: str) -> list[str]:
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
        target,
    ]


def mypy_count(source: str, filename: str) -> int:
    """mypy --strict error count for *source* checked as *filename*.

    With ``--follow-imports=silent`` mypy only reports errors in the file it
    was explicitly given, so every error line belongs to this file. Base and
    HEAD are both checked from a temp file so the comparison is symmetric.
    """
    suffix = Path(filename).name
    with tempfile.NamedTemporaryFile("w", suffix=f"__{suffix}", delete=False) as fh:
        fh.write(source)
        tmp = fh.name
    try:
        res = _run(mypy_command(tmp), env={**os.environ, "MYPYPATH": "apps/backend"})
        return sum(1 for line in res.stdout.splitlines() if _MYPY_ERROR_RE.match(line))
    finally:
        Path(tmp).unlink(missing_ok=True)


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="git ref to diff against")
    parser.add_argument("--tool", choices=["ruff", "mypy"], default="ruff")
    parser.add_argument("--package", default=PACKAGE_DEFAULT)
    args = parser.parse_args()

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

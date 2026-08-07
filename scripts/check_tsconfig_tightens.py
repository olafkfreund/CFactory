#!/usr/bin/env python3
"""A per-service tsconfig may only TIGHTEN the vendored fleet baseline.

Factory#546. ``standards/tsconfig.base.json`` opens with:

    Per-service tsconfig MUST extend this and may only TIGHTEN. Do not re-open
    holes (noImplicitAny:false etc. are forbidden).

The drift gate proves the VENDORED COPY still matches the hub. Nothing proved
the thing that actually compiles - ``apps/frontend-web/tsconfig.json`` - still
honours it. Without this check, adoption is one PR away from being undone in a
diff that reads as conformant: add ``"noUncheckedIndexedAccess": false`` to the
child, ``tsc`` goes green, every gate stays green, and the repo keeps an
``extends`` line that enforces nothing. That is the exact failure the issue
titled "partial adoption is not available" is about, so shipping ``extends``
without this check would only defer it.

Two things are asserted:

1. The child ``extends`` the vendored baseline (an ``extends``-less config
   inherits nothing, however strict it looks).
2. No flag the baseline turns ON is turned OFF by the child. Flags absent from
   the baseline are the child's own business, and a child turning something
   ON that the baseline leaves off is a tightening and allowed.

Usage:
    python scripts/check_tsconfig_tightens.py [--baseline P] [--config P]

Exit 0 when the child tightens-or-matches; 1 otherwise.
"""

# This is a CI command-line tool whose entire product is what it prints: the
# verdict and the offending flags go to the CI log and there is no other channel.
# Same scoped carve-out scripts/ratchet_lint.py already carries.
# ruff: noqa: T201

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Both files are hand-maintained JSON with no schema; `Any` is what json.loads
# returns and pretending otherwise would just move the cast around.
TsConfig = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "standards" / "tsconfig.base.json"
DEFAULT_CONFIG = REPO_ROOT / "apps" / "frontend-web" / "tsconfig.json"


def reopened_holes(baseline: TsConfig, config: TsConfig) -> list[str]:
    """Flags the baseline enables that the child disables, in baseline order."""
    base_opts: TsConfig = baseline.get("compilerOptions", {})
    child_opts: TsConfig = config.get("compilerOptions", {})
    return [
        flag for flag, value in base_opts.items() if value is True and child_opts.get(flag) is False
    ]


def check(baseline_path: Path, config_path: Path) -> list[str]:
    """Return the failure messages; empty means the child conforms."""
    baseline = json.loads(baseline_path.read_text())
    config = json.loads(config_path.read_text())
    failures = []

    extends = config.get("extends")
    # Compared as a resolved path, not a string: "../../standards/tsconfig.base.json"
    # and a future "./tsconfig.inherited.json" pointing elsewhere both need to land
    # on the real baseline file for the extends to mean anything.
    if extends is None or not isinstance(extends, str):
        failures.append(f"{config_path} has no `extends` - it inherits nothing from the baseline.")
    elif (config_path.parent / extends).resolve() != baseline_path.resolve():
        failures.append(
            f"{config_path} extends {extends!r}, which does not resolve to {baseline_path}."
        )

    for flag in reopened_holes(baseline, config):
        failures.append(
            f"{config_path} sets {flag!r} to false, which the baseline enables. "
            "The baseline is tighten-only: re-opening a hole is forbidden."
        )
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args()

    failures = check(args.baseline, args.config)
    for f in failures:
        print(f"TIGHTEN-ONLY VIOLATION: {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"{args.config} extends and does not re-open {args.baseline}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

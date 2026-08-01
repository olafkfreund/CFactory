"""A gate's name must not claim a scope the gate does not have (#267).

`code-quality.yml` declared a job called "ruff format --check (whole repo,
blocking)" that ran `ruff format --check --config ruff.toml apps/backend/cfactory`.
`apps/backend/run.py`, `apps/backend/migrations/**`, `tests/` and `scripts/` were
never checked. A gate whose name overstates its coverage is the thing that gets
cited in review as having read code it never read.

This asserts the property by READING the workflow, because the workflow's own
header comment made the same claim and was equally wrong -- a comment is not a
gate. Same defect and same check as PFactory#417 / PFactory PR#428.
"""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "code-quality.yml"

# Scope words a job name may use only if the run line really is that broad.
_OVERSTATEMENTS = ("whole repo", "whole-repo", "entire repo", "repo-wide", "all files")


def _format_job() -> tuple[str, str]:
    """The format-check job's declared `name:` and its `ruff format` run line."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    block = text.split("\n  format-check:", 1)[1]
    name = re.search(r"^\s*name:\s*(.+?)\s*$", block, re.M)
    run = re.search(r"^\s*run:\s*(ruff format .+?)\s*$", block, re.M)
    assert name is not None, "format-check job declares no name"
    assert run is not None, "format-check job runs no `ruff format`"
    return name.group(1), run.group(1)


def test_the_format_gate_does_not_claim_a_scope_it_lacks() -> None:
    name, run = _format_job()
    if not any(word in name.lower() for word in _OVERSTATEMENTS):
        return  # named honestly; nothing to reconcile

    raise AssertionError(
        f"the format job is called {name!r} but runs {run!r}. Either widen the "
        "scope to match the name, or name it after what it checks. Widening is "
        "not free here: apps/backend/runners/github/** is vendored and "
        "byte-identical drift-gated, so it can never be locally reformatted, and "
        "34 further files would be reformatted on the way (see #267)."
    )


def test_the_format_gate_names_the_path_it_checks() -> None:
    """The stronger half: the name must actually contain the checked path.

    Without this, "whole repo" could be swapped for any other vague phrase and
    the check above would pass while the gate stayed just as misleading.
    """
    name, run = _format_job()
    target = run.split()[-1].strip("\"'")
    # The workflow passes the path via ${PACKAGE_DIR}; resolve it to compare.
    if "PACKAGE_DIR" in target:
        env = re.search(r'^\s*PACKAGE_DIR:\s*"?([^"\s]+)', _WORKFLOW.read_text("utf-8"), re.M)
        assert env is not None, "PACKAGE_DIR is referenced but never declared"
        target = env.group(1)

    assert target in name, (
        f"the format job is called {name!r} but checks {target!r}. A reader has "
        "to be able to tell the gate's coverage from its name alone -- that is "
        "the only place it is seen on a PR."
    )

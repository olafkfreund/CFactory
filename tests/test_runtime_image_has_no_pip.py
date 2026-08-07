"""The runtime image must not ship pip (GHSA-6v7p-g79w-8964, CVE-2025-47273).

THE DEFECT THIS CLOSES. The deploy to `main` was blocked by its own Trivy gate
on two fixable HIGH findings:

    msgpack     GHSA-6v7p-g79w-8964  1.1.2   -> 1.2.1
    setuptools  CVE-2025-47273       70.3.0  -> 78.1.1

Neither is a CFactory dependency. `apps/backend/requirements.txt` names neither,
and neither appears in pip's "Successfully installed" line at build time. Both
come from *pip itself*: pip carries a private copy of its dependencies under
``pip/_vendor/`` and ships a CycloneDX SBOM at ``pip/_vendor/bom.cdx.json``
which Trivy reads (it logs "Third-party SBOM may lead to inaccurate
vulnerability detection" when it does). pip 26.2.1 -- the newest release, and
what ``python:3.14-slim`` bakes in -- pins ``msgpack==1.1.2`` and
``setuptools==70.3.0`` in ``_vendor/vendor.txt``.

So there was no manifest line to bump and no pip upgrade to take. The fix is to
delete pip after it has done its one job, which removes the vulnerable code
from the image rather than telling the scanner to look away. A `.trivyignore`
entry or `--severity` change would have turned the gate green while the image
was unchanged, which is the failure mode the gate exists to catch.

WHY THE UNINSTALL MUST SHARE THE INSTALL'S `RUN`. As its own layer it still
works, but the two can then drift: a later edit that reorders or
conditionally skips the second `RUN` silently restores pip, and the only thing
that notices is the deploy gate, after merge. Chained, they are one atomic
step and a diff that drops the uninstall is visibly a diff to the install line.

WHAT THIS TEST DOES AND DOES NOT PROVE. It reads the Dockerfile; it does not
build. It cannot tell you the built image is clean -- the Trivy step in
`deploy.yml` is what asserts that, and it is a hard `--exit-code 1` gate. What
this catches is the cheap, likely regression: somebody tidying the "redundant"
uninstall out of the Dockerfile months from now, with no idea it is load
bearing. That is worth a fast test in the PR that breaks it rather than a
20-minute image build.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO / "Dockerfile"


def _run_instructions(text: str) -> list[str]:
    """Every `RUN` instruction, line continuations folded into one string."""
    folded = re.sub(r"\\\s*\n\s*", " ", text)
    return [line.strip() for line in folded.splitlines() if line.strip().upper().startswith("RUN ")]


def test_pip_is_uninstalled_in_the_same_run_as_the_install() -> None:
    runs = _run_instructions(_DOCKERFILE.read_text(encoding="utf-8"))

    installs = [r for r in runs if "pip install" in r]
    assert installs, "expected a `pip install` step in the Dockerfile"

    for step in installs:
        assert re.search(r"pip uninstall\s+(--yes|-y)\b.*\bpip\b", step), (
            "the `pip install` step must chain `python -m pip uninstall --yes pip`.\n"
            "pip vendors msgpack 1.1.2 (GHSA-6v7p-g79w-8964) and setuptools 70.3.0\n"
            "(CVE-2025-47273) and advertises them to Trivy via pip/_vendor/bom.cdx.json,\n"
            "so leaving pip in the image re-blocks the deploy on two fixable HIGHs that\n"
            "no requirements.txt bump can reach. Offending step:\n"
            f"  {step}"
        )


def test_nothing_needs_pip_at_run_time() -> None:
    """The entrypoint must not depend on the package installer we just deleted."""
    text = _DOCKERFILE.read_text(encoding="utf-8")
    cmd = re.search(r"^CMD\s+(.+?)$", text, re.MULTILINE | re.DOTALL)
    assert cmd, "expected a CMD in the Dockerfile"
    assert "pip" not in cmd.group(1), (
        "CMD references pip, but pip is uninstalled during the build -- "
        "the container would fail to start."
    )

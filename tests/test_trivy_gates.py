"""The Trivy gates must scan what they claim, in the PR that breaks them (#327, #329).

TWO DEFECTS, ONE SHAPE.

#327 — `deploy.yml`'s lockfile step existed precisely to see bundled JS deps,
because the cockpit's node_modules never reaches the runtime image (only
`/app/dist` is copied into nginx) and so the image scan is blind to them. It ran
without `--include-dev-deps`, and Trivy suppresses devDependencies by default.
It reported `0` while four fixable HIGHs sat in the lockfile, one of them an
advisory Dependabot had open against that same file. The `dependencies` /
`devDependencies` split is a packaging convention, not a statement about what
Vite puts in `dist`, so the one step written to see bundled JS deps was
filtering out a category of bundled JS dep and calling the result clean. A gate
that measures less than its name claims is worse than an absent gate, because
the `0` is read as evidence (Factory#642).

#329 — `image-build.yml` built both Dockerfiles on every PR and never scanned
them. The repo's only Trivy gate ran on `push: branches: [main]`, i.e. after
merge, so a fixable HIGH first surfaced at deploy time and left `main`
merged-but-undeployed (#328).

WHY A TEST AND NOT A REVIEWER'S EYE. Both defects are invisible in a diff: the
first is an absent flag, the second an absent step, and neither makes anything
go red. The same argument tests/test_deploy_trigger_covers_baked_files.py makes
applies unchanged — a per-repo test fails in the PR that breaks it, and
Factory#525 settled that post-merge is a strictly worse place to find this class
of thing. That argument is the whole basis of #329, so this file had better be
willing to live by it.

WHAT THIS DOES NOT ASSERT. Not that the scans pass — that is the gates' own job,
in CI, against a live advisory feed. Only that they are still pointed at the
thing their names claim.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Copied from deploy.yml. Every flag here is load-bearing:
#   --severity HIGH,CRITICAL  the fleet's P0.8 policy
#   --ignore-unfixed          fail only on what can actually be fixed today
#   --exit-code 1             a finding is a gate, not a log line
_IMAGE_FLAGS = ("--severity", "HIGH,CRITICAL", "--ignore-unfixed", "--exit-code", "1")


def _trivy_commands(workflow: str) -> list[list[str]]:
    """Every `trivy ...` invocation in *workflow*, tokenised.

    Reads the parsed steps rather than grepping the file, so a `# trivy ...`
    that someone commented out reads as absent (which it is) instead of as
    present. Only lines that START with `trivy` are tokenised: the surrounding
    steps are ordinary shell and are not required to be shlex-parseable.
    """
    doc = yaml.safe_load((_WORKFLOWS / workflow).read_text(encoding="utf-8"))
    found: list[list[str]] = []
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run") or ""
            for line in run.replace("\\\n", " ").splitlines():
                if line.strip().startswith("trivy "):
                    found.append(shlex.split(line))
    return found


def _lockfile_scans(workflow: str) -> list[list[str]]:
    return [c for c in _trivy_commands(workflow) if c[1:2] == ["fs"]]


def _image_scans(workflow: str) -> list[list[str]]:
    return [c for c in _trivy_commands(workflow) if c[1:2] == ["image"]]


def test_lockfile_scans_include_dev_dependencies() -> None:
    """#327. The step exists to see bundled JS deps; dev deps are bundled JS deps."""
    scans = [c for w in ("deploy.yml", "image-build.yml") for c in _lockfile_scans(w)]
    assert scans, "no `trivy fs` lockfile scan in deploy.yml or image-build.yml"
    for command in scans:
        assert "--include-dev-deps" in command, (
            f"lockfile scan {' '.join(command)!r} omits --include-dev-deps, so Trivy "
            "suppresses devDependencies and the gate reports a 0 it has not earned. "
            "That is how #327 shipped: four fixable HIGHs, gate green. Do not remove "
            "the flag to make a finding go away -- fix the finding or record an "
            "audited exception in .trivyignore."
        )


def test_the_pr_gate_scans_both_images_it_builds() -> None:
    """#329. Building an image without scanning it is how a CVE reaches deploy."""
    scanned = {c[-1] for c in _image_scans("image-build.yml")}
    assert scanned == {"${BACKEND_TAG}", "${FRONTEND_TAG}"}, (
        f"image-build.yml builds both Dockerfiles but scans {sorted(scanned)}. Every "
        "image this gate builds must be scanned in the PR that proposes it; the "
        "deploy-time scan finds it after merge, which is the strictly worse place "
        "(Factory#525)."
    )


def test_the_pr_gate_and_the_deploy_gate_agree_on_what_clean_means() -> None:
    """Identical flags, or the two gates drift and the PR gate becomes theatre.

    A PR gate that is quietly laxer than the deploy gate is the worst of both:
    it is green on exactly the things that will block the deploy.
    """
    for workflow in ("deploy.yml", "image-build.yml"):
        for command in _image_scans(workflow):
            for flag in _IMAGE_FLAGS:
                assert flag in command, (
                    f"{workflow}: image scan {' '.join(command)!r} is missing {flag!r}. "
                    "Both gates run the same policy so a PR cannot be green on what "
                    "the deploy will reject."
                )


def test_the_deploy_gate_still_scans_the_pushed_digest() -> None:
    """The PR gate is defence in depth; it does not license weakening this one.

    deploy.yml scans by digest because that is the artifact that actually ships.
    The PR gate builds locally and scans a different, near-identical image, and
    cannot see drift between merge and deploy at all.
    """
    scanned = [c[-1] for c in _image_scans("deploy.yml")]
    assert len(scanned) == 2, f"deploy.yml scans {len(scanned)} images, expected 2"
    for ref in scanned:
        assert "@" in ref and "digest" in ref, (
            f"deploy.yml scans {ref!r}, which is not a digest reference. A tag can be "
            "repointed between the scan and the pull; a digest cannot."
        )

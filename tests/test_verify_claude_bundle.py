"""Offline checks for the backend image's bundled claude pin gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / ".github/scripts/verify-claude-bundle.sh"
_LOCK = (_ROOT / "apps/backend/claude-bundle.lock").read_text()
_VALUES = dict(
    line.split("=", 1) for line in _LOCK.splitlines() if "=" in line and not line.startswith("#")
)


@pytest.fixture
def verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lock = tmp_path / "claude-bundle.lock"
    lock.write_text(_LOCK)
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'if [[ "$1 $2" == "image inspect" ]]; then\n'
        '  printf "%s\\n" "${STUB_ARCH}"\n'
        'elif [[ "$1" == "run" ]]; then\n'
        '  if [[ "${STUB_MODE:-}" == "fail" ]]; then exit 42; fi\n'
        '  if [[ "${STUB_MODE:-}" == "missing_binary" ]]; then\n'
        '    echo "FileNotFoundError: _bundled/claude" >&2; exit 1\n'
        "  fi\n"
        '  printf "%s\\n" "${STUB_VERSION}"\n'
        '  if [[ "${STUB_MODE:-}" != "one_line" ]]; then printf "%s\\n" "${STUB_SHA}"; fi\n'
        "else exit 2; fi\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_ARCH", "amd64")
    monkeypatch.setenv("STUB_VERSION", _VALUES["version"])
    monkeypatch.setenv("STUB_SHA", _VALUES["sha256"])

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed repo script, with test-controlled Docker on PATH
            [str(_SCRIPT), "backend:test", str(lock)],
            capture_output=True,
            text=True,
            check=False,
        )

    return lock, run


def test_matching_bundle_passes(verifier) -> None:
    _, run = verifier
    result = run()
    assert result.returncode == 0, result.stderr
    assert _VALUES["version"] in result.stdout and _VALUES["sha256"] in result.stdout


@pytest.mark.parametrize("field", ["STUB_SHA", "STUB_VERSION"])
def test_bundle_mismatch_fails(verifier, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    _, run = verifier
    monkeypatch.setenv(field, "wrong")
    result = run()
    assert result.returncode == 1
    assert "expected" in result.stderr and "actual" in result.stderr
    assert "requirements.txt" in result.stderr and "claude-bundle.lock" in result.stderr


def test_missing_lock_fails(verifier) -> None:
    lock, run = verifier
    lock.unlink()
    result = run()
    assert result.returncode == 1
    assert "claude-bundle.lock" in result.stderr


@pytest.mark.parametrize("bad_lock", ["malformed line", "sha256=bad"])
def test_malformed_lock_fails(verifier, bad_lock: str) -> None:
    lock, run = verifier
    if bad_lock.startswith("sha256="):
        lock.write_text(_LOCK.replace(f"sha256={_VALUES['sha256']}", bad_lock))
    else:
        lock.write_text(_LOCK + bad_lock + "\n")
    result = run()
    assert result.returncode == 1
    assert "claude-bundle.lock" in result.stderr


@pytest.mark.parametrize("mode", ["fail", "missing_binary", "one_line"])
def test_docker_run_failure_or_missing_output_fails(
    verifier, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _, run = verifier
    monkeypatch.setenv("STUB_MODE", mode)
    assert run().returncode == 1


def test_non_amd64_image_fails(verifier, monkeypatch: pytest.MonkeyPatch) -> None:
    _, run = verifier
    monkeypatch.setenv("STUB_ARCH", "arm64")
    result = run()
    assert result.returncode == 1
    assert "arm64" in result.stderr

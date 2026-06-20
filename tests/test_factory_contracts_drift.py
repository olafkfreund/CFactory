"""Drift gate for the vendored factory-contracts package (Factory#160).

CFactory pins a BYTE-IDENTICAL copy of the Factory hub's generated single source
of truth at ``cfactory._contracts.factory_contracts``. This test fails if that
copy diverges from the hub's committed output, so the vendored module cannot
silently rot away from the pinned upstream.

Two layers:

1. A self-contained checksum lock on the vendored file (always runs). It pins the
   exact bytes of the copy that was reviewed; any local edit to the vendored
   module flips this red and forces a deliberate re-vendor + SHA bump.
2. A cross-repo diff against the hub source when the Factory repo is reachable as
   a sibling checkout (skipped otherwise). The blocking cross-repo enforcement in
   CI lives in the ``contracts-drift`` job in ``.github/workflows/code-quality.yml``
   (it checks the hub out at the pinned SHA and diffs); this test is the local /
   sibling-checkout convenience mirror.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

# The pinned hub source the vendored copy mirrors:
#   Factory/shared/factory-contracts/python/factory_contracts/__init__.py
#   @ 48f59a2c0a875f1637f4a442c1da4a9cd5fd1db9  (Factory#160 / PR #168)
_PINNED_HUB_SHA = "48f59a2c0a875f1637f4a442c1da4a9cd5fd1db9"

# sha256 of the vendored module's bytes at vendor time. Re-vendoring from the hub
# updates both this and the SHA above (and the header in _contracts/__init__.py).
_VENDORED_SHA256 = "a00d82375679acf7a49c07cdf70e8183f05213ae7ca92cbb1148b9d0d8debfed"

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
_VENDORED = _BACKEND / "cfactory" / "_contracts" / "factory_contracts" / "__init__.py"
# Sibling Factory checkout (…/GitHub/CFactory/tests -> …/GitHub/Factory).
# parents: [0]=tests [1]=CFactory [2]=GitHub.
_HUB_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "Factory"
    / "shared"
    / "factory-contracts"
    / "python"
    / "factory_contracts"
    / "__init__.py"
)


def test_vendored_module_matches_pinned_checksum() -> None:
    """The vendored bytes equal the reviewed, pinned copy (no silent local edit)."""
    digest = hashlib.sha256(_VENDORED.read_bytes()).hexdigest()
    assert digest == _VENDORED_SHA256, (
        "Vendored factory_contracts has been edited locally. It must stay a "
        f"byte-identical copy of the hub source @ {_PINNED_HUB_SHA}. Re-vendor "
        "from the hub and bump _VENDORED_SHA256 + the pinned SHA, or revert."
    )


def test_vendored_module_matches_hub_source_when_available() -> None:
    """When the Factory hub is a sibling checkout, the copy is byte-identical.

    Skips when the hub is not checked out alongside (the common CI case for this
    repo); the blocking cross-repo diff is enforced by the ``contracts-drift`` CI
    job, which checks the hub out at the pinned SHA.
    """
    if not _HUB_SOURCE.exists():
        pytest.skip("Factory hub not checked out as a sibling; see contracts-drift CI job")
    assert _VENDORED.read_bytes() == _HUB_SOURCE.read_bytes(), (
        "Vendored factory_contracts drifted from the hub source. Re-vendor and "
        f"bump the pinned SHA (currently {_PINNED_HUB_SHA})."
    )

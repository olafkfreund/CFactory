"""Shared fixtures: a hermetic WorkItemStore on a temp SQLite file, injected
into the app via the store_dep dependency override.

Also puts ``scripts/`` on sys.path so the lint ratchet is importable under test
(tests/test_ratchet_test_bar.py). It is a script directory, not a package, so
there is nothing to install — the same sibling-import arrangement it gets when
CI runs it as ``python3 scripts/ratchet_lint.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# APPENDED, not inserted: a script directory has no business shadowing a real
# first-party module.
sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))

from cfactory.app import create_app, store_dep
from cfactory.store import WorkItemStore


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def client(store):
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    return TestClient(app)

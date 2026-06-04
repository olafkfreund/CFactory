"""Shared fixtures: a hermetic WorkItemStore on a temp SQLite file, injected
into the app via the store_dep dependency override.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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

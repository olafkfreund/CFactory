"""Shared fixtures: a hermetic WorkItemStore on a temp SQLite file, injected
into the app via the store_dep dependency override.

Also puts ``scripts/`` on sys.path so the lint ratchet is importable under test
(tests/test_ratchet_test_bar.py). It is a script directory, not a package, so
there is nothing to install — the same sibling-import arrangement it gets when
CI runs it as ``python3 scripts/ratchet_lint.py``.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# APPENDED, not inserted: a script directory has no business shadowing a real
# first-party module.
sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))

from cfactory.app import create_app, store_dep
from cfactory.store import WorkItemStore


# RFC 6761 / RFC 2606 names the suite uses as stand-ins for a git host. They are
# reserved so they never resolve, which is exactly why they are safe to use as
# fakes -- and, since #412, why they need a resolver stub too.
_RESERVED_TEST_SUFFIXES = (".test", ".example.com", ".invalid", ".localhost")


@pytest.fixture(autouse=True)
def _reserved_test_hosts_resolve(monkeypatch):
    """Make the suite's fake git hosts resolvable, to loopback.

    The SSRF guard on a connection's ``base_url`` (#412) resolves the host before
    a credential is sent to it. Almost every test here points the board at
    ``https://gh.test`` or ``https://gitlab.test`` and swaps in an
    ``httpx.MockTransport``, so no packet is ever sent -- but the transport was
    the only half of the double that existed, and a mocked transport does not
    mock DNS. Without this the guard correctly refuses ~68 tests' fake hosts for
    not resolving, which is a true statement about a name that was never meant to.

    Loopback and not a public address on purpose: a fake host must not be able to
    stand in for one the guard would treat as ordinary. Tests that assert a
    REFUSAL use IP literals (169.254.169.254), which never reach a resolver.
    """
    real = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if isinstance(host, str) and host.endswith(_RESERVED_TEST_SUFFIXES):
            return real("127.0.0.1", port, *args, **kwargs)
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def client(store):
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: store
    return TestClient(app)

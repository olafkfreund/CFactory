"""The published capability matrix is true (RFC-0020 §3.5, Factory#366).

A matrix that is only prose drifts the first time a provider gains a method, and
the drift is silent and user-facing: someone picks GitLab on the strength of a
stale table. So the claims are asserted against the VENDORED provider layer
here. If a canonical provider grows a real ``enable_auto_merge``, or loses one,
these fail and the matrix has to be told.

Also covers the two surfaces the matrix is published on — REST and MCP — which
the parity law requires to agree.
"""

from __future__ import annotations

import inspect
import json

import pytest
from cfactory import auth, config
from cfactory.app import create_app
from cfactory.capabilities import (
    CAPABILITIES,
    FULL,
    NONE,
    PARTIAL,
    capability_matrix,
    supports,
)
from cfactory.git_config import AZURE_DEVOPS, GITHUB, GITLAB, SUPPORTED_PROVIDERS
from runners.github.providers.azure_devops_provider import AzureDevOpsProvider
from runners.github.providers.github_provider import GitHubProvider
from fastapi.testclient import TestClient
from runners.github.providers.gitlab_provider import GitLabProvider

# Not a credential: a read-scoped MCP key for the two-surface check below.
_READER = "capabilities-test-reader"

_PROVIDER_CLASS = {
    GITHUB: GitHubProvider,
    GITLAB: GitLabProvider,
    AZURE_DEVOPS: AzureDevOpsProvider,
}


def _raises_not_implemented(cls: type, method: str) -> bool:
    """True when ``cls.method`` is a body that only ever raises NotImplementedError.

    Read off the source rather than called, because calling would need a live
    host and a credential. A stub is short and unambiguous: the only statement
    that matters is the raise.
    """
    src = inspect.getsource(getattr(cls, method))
    body = src.split('"""')[-1] if '"""' in src else src
    return "raise NotImplementedError" in body


# ── the claims, checked against the vendored providers ───────────────────────


@pytest.mark.parametrize("provider", [GITLAB, AZURE_DEVOPS])
def test_auto_merge_is_absent_off_github(provider):
    """``enable_auto_merge`` is GitHub-shaped — the matrix says so, and it is."""
    assert supports(provider, "enable_auto_merge") == NONE
    assert _raises_not_implemented(_PROVIDER_CLASS[provider], "enable_auto_merge")


def test_auto_merge_is_real_on_github():
    assert supports(GITHUB, "enable_auto_merge") == FULL
    assert not _raises_not_implemented(GitHubProvider, "enable_auto_merge")


def test_assign_to_user_is_a_permanent_gap_on_azure_devops():
    """Not a backlog item: Azure DevOps has no coding agent to delegate to."""
    assert supports(AZURE_DEVOPS, "assign_to_user") == NONE
    assert _raises_not_implemented(AzureDevOpsProvider, "assign_to_user")


def test_assign_to_user_is_partial_on_gitlab_not_absent():
    """The correction the issue's own summary sentence needs.

    Factory#366 says ``assign_to_user`` "raises NotImplementedError on GitLab
    and Azure DevOps (GitLab Duo Workflow is partial)", which cannot be both.
    It does not raise on GitLab: it dispatches a Duo Workflow. What it does is
    silently no-op without a Duo entitlement and an OAuth-scoped token, which is
    a partial, and publishing it as absent would be as wrong in the other
    direction — a user who HAS Duo would be told a working feature is missing.
    """
    assert supports(GITLAB, "assign_to_user") == PARTIAL
    assert not _raises_not_implemented(GitLabProvider, "assign_to_user")


def test_the_things_that_do_work_everywhere_say_so():
    """Board sync, intake and PARR are the same on all three hosts (§3.5)."""
    for key in ("board_sync", "label_intake", "parr"):
        for provider in SUPPORTED_PROVIDERS:
            assert supports(provider, key) == FULL, f"{key} on {provider}"


def test_a_gitlab_tenant_gets_the_sentence_it_needs():
    """Every reduction carries an explanation, not just a level.

    "none" on its own sends someone hunting for a setting that does not exist.
    """
    for cap in CAPABILITIES:
        for provider, level in cap.support.items():
            if level in (PARTIAL, NONE):
                assert cap.notes.get(provider), f"{cap.key}/{provider} has no explanation"


def test_an_unknown_capability_reads_as_unsupported_rather_than_raising():
    """A caller asking "may I?" about something unknown must hear no."""
    assert supports(GITHUB, "teleportation") == NONE
    assert supports("bitbucket", "enable_auto_merge") == NONE


def test_every_provider_on_offer_is_in_every_row():
    """A provider the panel offers with no entry would render a blank cell."""
    matrix = capability_matrix()
    assert matrix.providers == list(SUPPORTED_PROVIDERS)
    for cap in matrix.capabilities:
        assert set(cap.support) == set(SUPPORTED_PROVIDERS), cap.key


# ── the two surfaces (RFC-0019 §3.3 parity law) ──────────────────────────────


@pytest.fixture
def surfaces(monkeypatch):
    """A client with a read-scoped MCP key, so both surfaces can be asked."""
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    auth.set_keys({_READER: {"read"}})
    yield TestClient(create_app())
    auth.reset_keystore()


_AUTH = {"Authorization": f"Bearer {_READER}"}


def _over_rest(client) -> dict:
    resp = client.get("/api/tenants/default/git-capabilities", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _over_mcp(client) -> dict:
    resp = client.post(
        "/mcp",
        headers=_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cfactory_git_capabilities", "arguments": {}},
        },
    )
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


def test_rest_and_mcp_publish_the_same_matrix(surfaces):
    """The parity law is about agreement, not just existence of both."""
    assert _over_rest(surfaces) == _over_mcp(surfaces)


def test_the_matrix_renders_for_each_provider(surfaces):
    """What the panel needs beside the selector: a level for whatever is picked."""
    body = _over_rest(surfaces)
    for provider in SUPPORTED_PROVIDERS:
        rendered = {cap["key"]: cap["support"][provider] for cap in body["capabilities"]}
        assert set(rendered.values()) <= {FULL, PARTIAL, NONE}
        assert rendered["parr"] == FULL
    gitlab = {cap["key"]: cap["support"][GITLAB] for cap in body["capabilities"]}
    assert gitlab["enable_auto_merge"] == NONE
    assert gitlab["assign_to_user"] == PARTIAL
    assert gitlab["board_sync"] == FULL

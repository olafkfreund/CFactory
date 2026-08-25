"""TFactory's ingest can self-register the project it is handed.

CFactory#438: `prepare_stage` sent TFactory an AIFactory `project_id` and no
`git_url`. The two services keep separate project registries, so every
card-driven test dispatch died on
``404 unknown project_id ... (and no git_url provided to self-register it)``.
"""

from __future__ import annotations

from cfactory.card_intake import prepare_stage
from cfactory.git_config import clone_url
from cfactory.models import Stage


class _Card:
    """The fields the TEST payload reads. Not the ORM Card -- this exercises
    payload construction, which needs no store, tenant or status."""

    def __init__(self, card_key="FCT-4", title="T", description="## Goal\n\nX\n"):
        self.card_key = card_key
        self.title = title
        self.description = description
        self.acceptance_criteria = ["AC one"]


def _payload(**kw):
    action = prepare_stage(_Card(), Stage.TEST, project_id="ai-proj", **kw)
    assert action is not None
    return action.payload


def test_test_payload_carries_git_url():
    p = _payload(git_url="https://github.com/acme/demo.git")
    assert p["git_url"] == "https://github.com/acme/demo.git"
    # still AIFactory's id -- TFactory self-registers rather than matching it
    assert p["project_id"] == "ai-proj"


def test_test_payload_omits_git_url_when_unresolved():
    """No configured project means no URL; a guessed host is worse than none."""
    assert "git_url" not in _payload()


def test_clone_url_uses_the_repo_host_not_the_api_host():
    """api.github.com serves the API; it does not serve git."""
    assert (
        clone_url("github", "https://api.github.com", "acme/demo")
        == "https://github.com/acme/demo.git"
    )


def test_clone_url_keeps_a_self_hosted_api_host():
    """GHE and GitLab serve API and repos from one host."""
    assert (
        clone_url("github", "https://ghe.acme.dev/api/v3", "acme/demo")
        == "https://ghe.acme.dev/acme/demo.git"
    )
    assert clone_url("gitlab", "https://gitlab.acme.dev", "g/p") == (
        "https://gitlab.acme.dev/g/p.git"
    )


def test_clone_url_returns_none_rather_than_guessing():
    assert clone_url("github", "https://api.github.com", None) is None
    assert clone_url("gitlab", None, "g/p") is None

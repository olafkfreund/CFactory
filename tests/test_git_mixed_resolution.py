"""RFC-0022 Phase 4: a connection may name another connection as its work-item side.

Single-provider connections are unchanged; a mixed one sends issue traffic only to
the work-item host with only its credential, and verify/dispatch only to the code
host with only its credential.
"""

from __future__ import annotations

import base64
import threading
from datetime import UTC, datetime

import httpx
import pytest
from cfactory import (
    cards as cards_module,
    config,
    git_config,
    git_config_ops,
    git_providers,
    github_sync,
    issue_import,
)
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.config import Settings
from cfactory.git_config import UNRESOLVED_PROVIDER, GitConfigError
from cfactory.git_connections import (
    GitConnectionCreate,
    GitConnectionUpdate,
    GitRepositoryCreate,
    GitRepositoryUpdate,
)
from cfactory.git_providers import build_provider
from runners.github.providers.protocol import IssueData
from sqlalchemy import text

_KEY = base64.b64encode(b"p9" * 16).decode()
_GH_TOKEN = "ghp-CODE-SIDE-TOKEN-0001"  # noqa: S105 — a fake, not a secret
_GL_TOKEN = "glpat-WORK-ITEM-TOKEN-0002"  # noqa: S105 — a fake, not a secret
_ENV_TOKEN = "env-DEPLOYMENT-TOKEN-0003"  # noqa: S105 — a fake, not a secret
_GL_HOST = "https://gitlab.example.com"
_CODE = "acme/widgets"
_WORK = "acme/group/board"


class Host:
    """A recording GitHub host (the real HTTP provider, over a mock transport)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "number": 11,
                    "title": "t",
                    "body": "",
                    "state": "open",
                    "labels": [],
                    "html_url": "https://h.test/11",
                },
            )
        return httpx.Response(200, json=[] if "issues" in request.url.path else {"name": "x"})

    def secrets(self) -> list[str]:
        return [
            v
            for r in self.requests
            for k, v in r.headers.items()
            if k.lower() in {"authorization", "private-token"}
        ]


class FakeGitLab:
    """Stands in for the canonical GitLab provider, which takes no transport."""

    built: list[FakeGitLab] = []

    def __init__(self, project: str, *, _token: str | None = None, _base_url: str | None = None):
        self.project, self.token, self.base_url = project, _token, _base_url
        self.calls: list[str] = []
        FakeGitLab.built.append(self)

    async def create_issue(self, title, body, labels=None, assignees=None):
        self.calls.append("create_issue")
        now = datetime.now(UTC)
        return IssueData(11, title, body, "a", "open", [], now, now, "https://gl.test/11")

    async def fetch_issue(self, number):
        self.calls.append("fetch_issue")
        raise AssertionError("not used")

    async def fetch_issues(self, filters=None):
        self.calls.append("fetch_issues")
        return []

    async def fetch_comments_bulk(self, numbers, since=None):
        self.calls.append("fetch_comments_bulk")
        return {}

    async def get_repository_info(self):
        self.calls.append("get_repository_info")
        return {"name": "x"}


class Router:
    """The two hosts: real GitHub provider over a transport, fake GitLab provider."""

    def __init__(self) -> None:
        self.github = Host()
        FakeGitLab.built = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.github.handler)

    @property
    def gitlab(self) -> list[FakeGitLab]:
        return FakeGitLab.built

    def gitlab_calls(self) -> list[str]:
        return [c for p in FakeGitLab.built for c in p.calls]

    def total(self) -> int:
        return len(self.github.requests) + len(FakeGitLab.built)


@pytest.fixture
def settings():
    return Settings(credential_key=f"v1:{_KEY}")


@pytest.fixture(autouse=True)
def _settings(monkeypatch, settings):
    for module in (cards_module, git_config, git_config_ops, github_sync, issue_import):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)


@pytest.fixture
def cards(tmp_path):
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr(
        git_providers,
        "get_provider",
        lambda _type, project, **kw: FakeGitLab(project, **kw),
    )
    return Router()


def _mixed(cards, *, work_token: str | None = _GL_TOKEN, code_token: str | None = _GH_TOKEN):
    """GitHub code connection whose work items live on a GitLab connection."""
    gitlab = cards.create_connection(GitConnectionCreate(provider="gitlab", base_url=_GL_HOST))
    github = cards.create_connection(
        GitConnectionCreate(provider="github", work_item_connection_id=gitlab.id)
    )
    if work_token:
        cards.set_connection_credential(gitlab.id, work_token)
    if code_token:
        cards.set_connection_credential(github.id, code_token)
    repo = cards.create_repository(
        github.id, GitRepositoryCreate(project=_CODE, work_item_project=_WORK, make_default=True)
    )
    return github, gitlab, repo


# ── single provider: unchanged ───────────────────────────────────────────────


def test_a_connection_without_a_work_item_side_resolves_exactly_as_before(cards, router):
    github = cards.create_connection(GitConnectionCreate(provider="github"))
    cards.set_connection_credential(github.id, _GH_TOKEN)
    cards.create_repository(github.id, GitRepositoryCreate(project=_CODE, make_default=True))

    target = cards.git_target()
    assert target.work_item is None
    assert target.work_item_side is target
    assert (target.provider, target.project) == ("github", _CODE)

    card = cards.create(CardCreate(title="Plain"))
    result = github_sync.sync_card(cards, card, transport=router.transport())
    assert result["ok"] is True and result["issue_ref"] == f"{_CODE}#11"
    assert router.gitlab == []
    assert router.github.secrets() and all(_GH_TOKEN in v for v in router.github.secrets())


def test_existing_rows_have_no_work_item_side(cards):
    github = cards.create_connection(GitConnectionCreate(provider="github"))
    assert github.work_item_connection_id is None
    repo = cards.create_repository(github.id, GitRepositoryCreate(project=_CODE))
    assert repo.work_item_project is None


# ── mixed resolution ─────────────────────────────────────────────────────────


def test_a_mixed_connection_resolves_each_side_from_its_own_connection(cards):
    github, gitlab, _ = _mixed(cards)
    target = cards.git_target()
    side = target.work_item_side
    assert (target.provider, target.project, target.base_url) == (
        "github",
        _CODE,
        "https://api.github.com",
    )
    assert (side.provider, side.project, side.base_url) == ("gitlab", _WORK, _GL_HOST)
    assert side.connection_id == gitlab.id and target.connection_id == github.id
    # a card's issue_ref names the WORK-ITEM project, and finds this repository
    assert cards.resolve_repository(project=_WORK) is not None
    assert cards.resolve_repository(project=_WORK).repository.project == _CODE


def test_issue_traffic_goes_only_to_the_work_item_host_with_only_its_credential(cards, router):
    _mixed(cards)
    card = cards.create(CardCreate(title="Plan it"))
    result = github_sync.sync_card(cards, card, transport=router.transport())
    assert result["ok"] is True, result
    assert router.github.requests == []
    assert router.gitlab_calls() == ["create_issue"]
    assert {p.token for p in router.gitlab} == {_GL_TOKEN}
    assert router.gitlab[0].base_url == _GL_HOST and router.gitlab[0].project == _WORK
    assert result["issue_ref"] == f"{_WORK}#11"


def test_import_reads_only_the_work_item_host(cards, router):
    _mixed(cards)
    result = issue_import.import_issues(cards, transport=router.transport())
    assert result["ok"] is True, result
    assert router.github.requests == []
    assert "fetch_issues" in router.gitlab_calls()
    assert {p.token for p in router.gitlab} == {_GL_TOKEN}


def test_verify_and_dispatch_use_only_the_code_side(cards, router, tmp_path):
    github, _, repo = _mixed(cards)
    audit = _audit(tmp_path)
    git_config_ops.verify_git_connection(
        cards, AuditContext(audit, "t"), github.id, transport=router.transport()
    )
    assert router.gitlab == []
    assert router.github.requests
    assert all(_GH_TOKEN in s for s in router.github.secrets())
    card = cards.create(CardCreate(title="Build", repository_id=repo.id))
    from cfactory.card_intake import card_clone_url, repo_ref

    settings = config.get_settings()
    assert _CODE in (repo_ref(cards, settings, card) or "")
    assert _WORK not in (repo_ref(cards, settings, card) or "")
    assert "github.com" in (card_clone_url(cards, settings, card) or "")


def _audit(tmp_path):
    from cfactory.audit import AuditStore

    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="p9-test-hmac")  # noqa: S106


# ── credentials never cross ──────────────────────────────────────────────────


def test_the_deployment_token_is_never_sent_to_the_work_item_provider(cards, router, monkeypatch):
    env = Settings(credential_key=f"v1:{_KEY}", github_token=_ENV_TOKEN)
    for module in (cards_module, git_config, git_config_ops, github_sync, issue_import):
        monkeypatch.setattr(module, "get_settings", lambda: env, raising=False)
    _mixed(cards, work_token=None, code_token=None)
    target = cards.git_target(env)
    assert target.credential.configured  # the code side still has the env fallback
    assert not target.work_item_side.credential.configured
    card = cards.create(CardCreate(title="No token on the work side"))
    result = github_sync.sync_card(cards, card, settings=env, transport=router.transport())
    assert result["synced"] is False and router.total() == 0


# ── a dangling reference fails visibly and never falls back ──────────────────


def test_a_dangling_reference_fails_without_any_outbound_call(cards, router):
    github, gitlab, _ = _mixed(cards)
    with cards._engine.begin() as conn:  # a manual DB edit, the only way to dangle
        conn.execute(text("DELETE FROM git_connection WHERE id = :i"), {"i": gitlab.id})
    target = cards.git_target()
    assert target.work_item_side.provider == UNRESOLVED_PROVIDER
    card = cards.create(CardCreate(title="Dangling"))
    result = github_sync.sync_card(cards, card, transport=router.transport())
    assert result["ok"] is False
    assert router.total() == 0
    with pytest.raises(ValueError, match="unknown git provider"):
        build_provider(target.work_item_side, "a/b")
    imported = issue_import.import_issues(cards, transport=router.transport())
    assert imported["ok"] is False and router.total() == 0


# ── validation ───────────────────────────────────────────────────────────────


def test_reference_validation(cards):
    a = cards.create_connection(GitConnectionCreate(provider="github"))
    b = cards.create_connection(GitConnectionCreate(provider="gitlab", base_url=_GL_HOST))
    with pytest.raises(GitConfigError, match="its own"):
        cards.update_connection(a.id, GitConnectionUpdate(work_item_connection_id=a.id))
    with pytest.raises(GitConfigError, match="no git connection"):
        cards.update_connection(a.id, GitConnectionUpdate(work_item_connection_id=999))
    cards.update_connection(a.id, GitConnectionUpdate(work_item_connection_id=b.id))
    # no chains in either direction
    c = cards.create_connection(GitConnectionCreate(provider="gitlab", base_url="https://gl2.test"))
    with pytest.raises(GitConfigError, match="no chains"):
        cards.update_connection(b.id, GitConnectionUpdate(work_item_connection_id=c.id))
    with pytest.raises(GitConfigError, match="no chains"):
        cards.update_connection(c.id, GitConnectionUpdate(work_item_connection_id=a.id))


def test_another_tenants_connection_cannot_be_referenced(cards):
    other = cards.scoped("other")
    foreign = other.create_connection(GitConnectionCreate(provider="gitlab", base_url=_GL_HOST))
    with pytest.raises(GitConfigError, match="no git connection"):
        cards.create_connection(
            GitConnectionCreate(provider="github", work_item_connection_id=foreign.id)
        )


def test_a_referenced_connection_cannot_be_deleted_or_moved(cards):
    github, gitlab, _ = _mixed(cards)
    with pytest.raises(GitConfigError, match="detach"):
        cards.delete_connection(gitlab.id)
    with pytest.raises(GitConfigError, match="detach"):
        cards.update_connection(gitlab.id, GitConnectionUpdate(base_url="https://moved.test"))
    cards.update_connection(github.id, GitConnectionUpdate(work_item_connection_id=None))
    assert cards.repository(cards.repositories()[0].id).work_item_project is None
    assert cards.delete_connection(gitlab.id) is True


def test_work_item_project_needs_a_work_item_connection(cards):
    github = cards.create_connection(GitConnectionCreate(provider="github"))
    with pytest.raises(GitConfigError, match="needs a work-item connection"):
        cards.create_repository(
            github.id, GitRepositoryCreate(project=_CODE, work_item_project=_WORK)
        )


# ── existing cards pin the work-item side ────────────────────────────────────


def test_the_side_cannot_move_while_cards_track_issues_there(cards):
    github, gitlab, repo = _mixed(cards)
    cards.create(CardCreate(title="Tracked", issue_ref=f"{_WORK}#4"))
    with pytest.raises(GitConfigError, match="1 card"):
        cards.update_connection(github.id, GitConnectionUpdate(work_item_connection_id=None))
    with pytest.raises(GitConfigError, match="1 card"):
        cards.update_repository(repo.id, GitRepositoryUpdate(work_item_project="acme/group/other"))


def test_attaching_a_side_is_refused_while_cards_track_the_code_project(cards):
    github = cards.create_connection(GitConnectionCreate(provider="github"))
    cards.create_repository(github.id, GitRepositoryCreate(project=_CODE, make_default=True))
    cards.create(CardCreate(title="Tracked", issue_ref=f"{_CODE}#4"))
    gitlab = cards.create_connection(GitConnectionCreate(provider="gitlab", base_url=_GL_HOST))
    with pytest.raises(GitConfigError, match="1 card"):
        cards.update_connection(github.id, GitConnectionUpdate(work_item_connection_id=gitlab.id))


# ── concurrency: the lock is taken before the invariants are read ────────────


def test_a_second_writer_waits_for_the_lock_on_the_referenced_row(tmp_path):
    url = f"sqlite:///{tmp_path / 'race.db'}"
    one, two = CardStore(url), CardStore(url)
    a = one.create_connection(GitConnectionCreate(provider="github"))
    b = one.create_connection(GitConnectionCreate(provider="gitlab", base_url=_GL_HOST))
    outcome: list[object] = []

    with one._session.begin() as session:
        one._lock_connections(session, [a.id, b.id])  # writer one holds the lock

        def second() -> None:
            try:
                two.delete_connection(b.id)
                outcome.append("deleted")
            except Exception as exc:  # noqa: BLE001 — recorded for the assertion
                outcome.append(type(exc).__name__)

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive() or outcome  # blocked on the lock, or refused
        session.execute(
            text("UPDATE git_connection SET work_item_connection_id = :b WHERE id = :a"),
            {"a": a.id, "b": b.id},
        )
    thread.join(timeout=20)
    # the reference landed first, so the delete is refused, not applied
    assert outcome == ["GitConfigError"], outcome


def test_a_chain_forming_writer_waits_and_is_refused(tmp_path):
    url = f"sqlite:///{tmp_path / 'chain.db'}"
    one, two = CardStore(url), CardStore(url)
    a = one.create_connection(GitConnectionCreate(provider="github"))
    b = one.create_connection(GitConnectionCreate(provider="gitlab", base_url=_GL_HOST))
    c = one.create_connection(GitConnectionCreate(provider="gitlab", base_url="https://gl2.test"))
    outcome: list[str] = []

    with one._session.begin() as session:
        one._lock_connections(session, [a.id, b.id])

        def second() -> None:
            try:
                two.update_connection(b.id, GitConnectionUpdate(work_item_connection_id=c.id))
                outcome.append("attached")
            except GitConfigError as exc:
                outcome.append(str(exc))

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=0.5)
        session.execute(
            text("UPDATE git_connection SET work_item_connection_id = :b WHERE id = :a"),
            {"a": a.id, "b": b.id},
        )
    thread.join(timeout=20)
    assert outcome and "no chains" in outcome[0], outcome


def test_attaching_a_side_checks_existing_intake_paths_against_the_new_provider(cards):
    github = cards.create_connection(GitConnectionCreate(provider="github"))
    cards.create_repository(
        github.id,
        GitRepositoryCreate(project=_CODE, intake_project="acme/other", make_default=True),
    )
    ado = cards.create_connection(
        GitConnectionCreate(provider="azure_devops", base_url="https://dev.azure.com")
    )
    with pytest.raises(GitConfigError, match="intake_project"):
        cards.update_connection(github.id, GitConnectionUpdate(work_item_connection_id=ado.id))
    assert cards.connection(github.id).work_item_connection_id is None


def test_a_repository_guard_covers_every_tracked_project_path(cards):
    _, _, repo = _mixed(cards)
    cards.create(CardCreate(title="Tracked on the code path", issue_ref=f"{_CODE}#4"))
    with pytest.raises(GitConfigError, match="1 card"):
        cards.update_repository(repo.id, GitRepositoryUpdate(work_item_project="acme/group/other"))


def test_replacing_the_side_checks_retained_work_item_paths(cards):
    github, _, repo = _mixed(cards)
    cards.update_repository(repo.id, GitRepositoryUpdate(work_item_project="acme/issues"))
    ado = cards.create_connection(
        GitConnectionCreate(provider="azure_devops", base_url="https://dev.azure.com")
    )
    with pytest.raises(GitConfigError, match="work_item_project"):
        cards.update_connection(github.id, GitConnectionUpdate(work_item_connection_id=ado.id))


def test_an_import_does_not_persist_issues_after_the_side_moved(cards, router, monkeypatch):
    _mixed(cards)
    real = FakeGitLab.fetch_issues

    async def moved_during_fetch(self, filters=None):
        cards.update_connection(
            cards.connections()[1].id, GitConnectionUpdate(work_item_connection_id=None)
        )
        return await real(self, filters)

    monkeypatch.setattr(FakeGitLab, "fetch_issues", moved_during_fetch)
    result = issue_import.import_issues(cards, transport=router.transport())
    assert result["ok"] is False and "changed during the import" in result["reason"]

"""Step 1 of #561: the card kind, the repository test command and the store helpers."""

from __future__ import annotations

import pytest
from cfactory import auth, config
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import Card, CardCreate, CardRow, CardStore
from cfactory.db import alembic_config, bootstrap_schema
from cfactory.git_config import GitConfigError
from cfactory.git_connections import (
    GitConnectionCreate,
    GitRepositoryCreate,
    GitRepositoryUpdate,
)
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

_KEY = "k" * 32
_CARD_FIELDS = Card.model_json_schema()
_REQUEST = "why is the build slow?"


@pytest.fixture
def store(tmp_path) -> CardStore:
    return CardStore(f"sqlite:///{tmp_path / 'cards.db'}")


def _repo(store: CardStore) -> int:
    conn = store.create_connection(GitConnectionCreate(provider="github"))
    return store.create_repository(conn.id, GitRepositoryCreate(project="acme/widgets")).id


def _investigation(store: CardStore, repo_id: int) -> Card:
    return store.create_investigation_card(
        title="Investigate: x", description=_REQUEST, repository_id=repo_id, binding={"repo": "a"}
    )


def test_kind_survives_every_loader(store):
    repo_id = _repo(store)
    inv = _investigation(store, repo_id)
    assert inv.is_investigation
    assert inv.tier is None and inv.issue_ref is None and inv.correlation_key is None
    # Seed the lookup keys on the investigation row so every loader can find it.
    with store._session.begin() as session:
        row = store._get_row(session, inv.card_key)
        row.issue_ref = "acme/widgets#7"
        row.correlation_key = "corr-1"
    work = store.create(CardCreate(title="plain"))
    assert not work.is_investigation
    assert store.get(inv.card_key).is_investigation
    assert store.get_by_issue_ref("acme/widgets#7").is_investigation
    assert store.get_by_correlation_key("corr-1").is_investigation
    assert store.cards_for_project("acme/widgets")[0].is_investigation
    listed = {c.card_key: c.is_investigation for c in store.list()}
    assert listed == {inv.card_key: True, work.card_key: False}
    assert store.update(inv.card_key, {"priority": 3}).is_investigation
    assert not store.update(work.card_key, {"priority": 3}).is_investigation


def test_card_wire_shape_is_unchanged():
    assert "kind" not in _CARD_FIELDS["properties"]
    assert "kind" not in CardCreate.model_json_schema()["properties"]
    card = Card.model_validate(
        {
            "card_key": "A-1", "tenant_id": "default", "title": "t", "acceptance_criteria": [],
            "status": "backlog", "priority": 0, "tier": None, "assignee": None,
            "milestone": None, "correlation_key": None, "issue_ref": None, "issue_state": None,
            "labels": [], "github_sync_error": None, "stage_runs": {},
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        }
    )  # fmt: skip
    assert "kind" not in card.model_dump()


def test_existing_rows_read_as_work_after_late_column_upgrade(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    CardStore(url)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE cards DROP COLUMN kind"))
        conn.execute(
            text(
                "INSERT INTO cards (card_key, tenant_id, title, acceptance_criteria, status,"
                " priority, labels, stage_runs, created_at, updated_at) VALUES"
                " ('OLD-1','default','t','[]','backlog',0,'[]','{}','2026-01-01','2026-01-01')"
            )
        )
    reopened = CardStore(url)  # _ensure_late_columns adds kind
    assert reopened.get("OLD-1") is not None and not reopened.get("OLD-1").is_investigation
    with reopened._session() as session:
        assert session.scalars(text("SELECT kind FROM cards")).all() == ["work"]


def test_migration_adds_both_columns_with_one_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    from alembic import command  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    assert len(ScriptDirectory.from_config(alembic_config(url)).get_heads()) == 1
    bootstrap_schema(url)
    inspector = inspect(create_engine(url))
    assert "kind" in {c["name"] for c in inspector.get_columns("cards")}
    assert "investigate_test_command" in {
        c["name"] for c in inspector.get_columns("git_repository")
    }
    command.downgrade(alembic_config(url), "7e4c2a91d6b8")
    inspector = inspect(create_engine(url))
    assert "kind" not in {c["name"] for c in inspector.get_columns("cards")}


def test_include_deleted(store):
    inv = _investigation(store, _repo(store))
    deleted = store.delete(inv.card_key)
    assert deleted
    assert store.get(inv.card_key) is None
    assert store.get(inv.card_key, include_deleted=True).is_investigation


def test_stage_runs_changes_persist_under_the_lock(store):
    inv = _investigation(store, _repo(store))
    assert inv.stage_runs["investigate"]["detail"]["binding"] == {"repo": "a"}
    with store.locked_investigation(inv.card_key, inv.tenant_id) as (session, row):
        assert row is not None
        store.write_investigation(session, row, {"status": "running", "detail": {"run_id": "r1"}})
    assert store.get(inv.card_key).stage_runs["investigate"]["detail"]["run_id"] == "r1"
    with store.locked_investigation("NOPE-1", inv.tenant_id) as (_session, row):
        assert row is None
    store.delete(inv.card_key)
    with store.locked_investigation(inv.card_key, inv.tenant_id) as (_session, row):
        assert row is not None and row.deleted_at is not None  # deleted cards included


@pytest.mark.parametrize(("sent", "stored"), [("", None), ("pytest -q", "pytest -q")])
def test_command_normalisation(store, sent, stored):
    repo_id = _repo(store)
    previous: dict = {}
    row = store.update_repository(
        repo_id, GitRepositoryUpdate(investigate_test_command=sent), previous=previous
    )
    assert row.investigate_test_command == stored
    assert previous == {"investigate_test_command": None}


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b", "x" * 1001])
def test_command_rejected(store, bad):
    with pytest.raises((GitConfigError, ValueError)):
        store.update_repository(_repo(store), GitRepositoryUpdate(investigate_test_command=bad))


def test_command_rest_round_trip(store, tmp_path, monkeypatch):
    audit = AuditStore(f"sqlite:///{tmp_path / 'a.db'}", hmac_secret="inv-store-test")  # noqa: S106
    monkeypatch.delenv("CFACTORY_MCP_SECRET", raising=False)
    config.reset_settings()
    auth.set_keys({_KEY: {"read", "write"}})
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: store
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = lambda: None
    client = TestClient(app, headers={"Authorization": f"Bearer {_KEY}"})
    try:
        repo_id = _repo(store)
        url = f"/api/tenants/default/git-repositories/{repo_id}"
        resp = client.patch(url, json={"investigate_test_command": "pytest -q"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["investigate_test_command"] == "pytest -q"
        resp = client.patch(url, json={"investigate_test_command": ""})
        assert resp.status_code == 200, resp.text
        assert resp.json()["investigate_test_command"] is None
    finally:
        auth.reset_keystore()


def test_card_row_kind_defaults_to_work(store):
    with store._session.begin() as session:
        session.add(CardRow(card_key="R-1", title="t"))
    assert store.get("R-1") is not None and not store.get("R-1").is_investigation

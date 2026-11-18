"""Mixed-tenant schema migration checks."""

from alembic import command
from alembic.script import ScriptDirectory
from cfactory.db import alembic_config
from sqlalchemy import create_engine, inspect, text
from test_schema_bootstrap import _bootstrap_with_create_all, _schema


def test_migration_adds_nullable_columns_and_downgrades_without_losing_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'mixed.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "b8d47e1f2a63")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO git_connection (id, label) VALUES (101, 'existing')"))
        conn.execute(
            text(
                "INSERT INTO git_repository (id, connection_id, project) "
                "VALUES (202, 101, 'existing/repo')"
            )
        )

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT label, work_item_connection_id FROM git_connection WHERE id = 101")
        ).one() == ("existing", None)
        assert conn.execute(
            text("SELECT project, work_item_project FROM git_repository WHERE id = 202")
        ).one() == ("existing/repo", None)

    command.downgrade(cfg, "-1")
    inspector = inspect(engine)
    assert "work_item_connection_id" not in {
        c["name"] for c in inspector.get_columns("git_connection")
    }
    assert "work_item_project" not in {c["name"] for c in inspector.get_columns("git_repository")}
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT label FROM git_connection WHERE id = 101")).scalar_one()
            == "existing"
        )
        assert (
            conn.execute(text("SELECT project FROM git_repository WHERE id = 202")).scalar_one()
            == "existing/repo"
        )
    engine.dispose()


def test_migration_has_one_head_and_matches_create_all(tmp_path):
    migrated = f"sqlite:///{tmp_path / 'migrated.db'}"
    created = f"sqlite:///{tmp_path / 'created.db'}"
    cfg = alembic_config(migrated)
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1
    command.upgrade(cfg, "head")
    _bootstrap_with_create_all(created)
    assert _schema(migrated) == _schema(created)

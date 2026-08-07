"""The schema is under Alembic control, and adopting an existing one is safe (#308).

Until this, nothing in the image, the chart or the deploy workflow ran
``alembic upgrade head``. The live cockpit's database had no ``alembic_version``
table at all: its schema was whatever ``Base.metadata.create_all`` produced at
store init. A migration was therefore a check that could not fail -- writeable,
reviewable, mergeable, and never applied. That is what ruled out a unique index
as the structural fix for the audit chain race in #306.

The load-bearing tests here are the two that make the ADOPTION honest:

* ``test_the_migrations_and_the_models_agree`` -- ``alembic upgrade head`` and
  the app's own ``create_all`` bootstrap produce the same schema. This is what
  makes ``head`` the right revision to stamp an existing database at. If the two
  ever diverge, the stamp starts lying and this test says so first.
* ``test_an_unstamped_database_collides_and_a_stamped_one_does_not`` -- the
  mutation check. Skip the stamp and the very next ``alembic upgrade head``
  fails on a table that already exists, which is precisely the production
  breakage the stamp exists to prevent.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
from cfactory.db import VERSION_TABLE, alembic_config, bootstrap_schema
from cfactory.store import WorkItemStore
from sqlalchemy import create_engine, inspect, text

_HMAC = "schema-bootstrap-test-anchor"  # noqa: S105 — a test constant, not a credential


@pytest.fixture
def url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'cfactory.db'}"


def _bootstrap_with_create_all(url: str) -> None:
    """Build a database exactly the way every deployed CFactory has: the stores'
    own ``create_all`` plus the live-DB column guards, and no Alembic."""
    WorkItemStore(url)
    CardStore(url)
    AuditStore(url, hmac_secret=_HMAC)


def _schema(url: str) -> dict[str, dict]:
    """Tables, columns and indexes. Schema only -- no row is ever read."""
    inspector = inspect(create_engine(url))
    return {
        table: {
            "columns": {c["name"]: str(c["type"]) for c in inspector.get_columns(table)},
            "indexes": {
                i["name"]: (tuple(i["column_names"]), bool(i.get("unique")))
                for i in inspector.get_indexes(table)
            },
        }
        for table in inspector.get_table_names()
        if table != VERSION_TABLE
    }


def _stamped_revision(url: str) -> str | None:
    engine = create_engine(url)
    if VERSION_TABLE not in inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text(f"select version_num from {VERSION_TABLE}")).scalar()  # noqa: S608 — a module constant, not input


def _head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


# ── the three cases ──────────────────────────────────────────────────────────


def test_an_empty_database_is_created_by_the_migrations(url):
    assert bootstrap_schema(url) == "created"
    assert _stamped_revision(url) == _head_revision()
    assert "cards" in _schema(url)


def test_an_existing_unstamped_database_is_adopted_not_replayed(url):
    _bootstrap_with_create_all(url)
    before = _schema(url)
    assert _stamped_revision(url) is None, "the state #308 reports on the live cockpit"

    assert bootstrap_schema(url) == "adopted"

    assert _stamped_revision(url) == _head_revision()
    # Nothing was dropped, recreated or altered: adoption is a bookkeeping write.
    assert _schema(url) == before


def test_a_stamped_database_is_upgraded_and_a_second_call_is_a_no_op(url):
    bootstrap_schema(url)
    assert bootstrap_schema(url) == "upgraded"
    assert bootstrap_schema(url) == "upgraded"
    assert _stamped_revision(url) == _head_revision()


# ── what makes the stamp honest ──────────────────────────────────────────────


def test_the_migrations_and_the_models_agree(tmp_path):
    """`alembic upgrade head` and `create_all` must describe the same database.

    They are two independent stories about one schema and only one of them has
    ever run in production. Stamping the live database at head asserts that the
    story it was built from and the story the migrations tell are the same one --
    so that claim is checked here rather than assumed.
    """
    migrated = f"sqlite:///{tmp_path / 'migrated.db'}"
    created = f"sqlite:///{tmp_path / 'created.db'}"
    bootstrap_schema(migrated)
    _bootstrap_with_create_all(created)

    assert _schema(migrated) == _schema(created)


def test_adopting_leaves_no_revision_pending(url):
    """After adoption the database is AT head, not merely labelled with it: a
    following upgrade has nothing to do and must not touch the schema."""
    _bootstrap_with_create_all(url)
    bootstrap_schema(url)
    before = _schema(url)

    command.upgrade(alembic_config(url), "head")

    assert _schema(url) == before


# ── the mutation check ───────────────────────────────────────────────────────


def test_an_unstamped_database_collides_and_a_stamped_one_does_not(tmp_path):
    """Remove the stamp and the deploy breaks -- which is why the stamp is there.

    The left half is what would happen if this change had wired
    `alembic upgrade head` WITHOUT adopting the existing schema first: the first
    revision tries to create a table that create_all already made, and the
    upgrade dies. The right half is the same database, stamped, upgrading
    cleanly.
    """
    unstamped = f"sqlite:///{tmp_path / 'unstamped.db'}"
    _bootstrap_with_create_all(unstamped)
    with pytest.raises(Exception, match="(?i)already exists"):
        command.upgrade(alembic_config(unstamped), "head")

    stamped = f"sqlite:///{tmp_path / 'stamped.db'}"
    _bootstrap_with_create_all(stamped)
    bootstrap_schema(stamped)
    command.upgrade(alembic_config(stamped), "head")  # no raise


# ── the step actually runs where it has to ───────────────────────────────────


def test_the_app_runs_the_bootstrap_before_it_builds_a_store(monkeypatch, tmp_path):
    """A migration that lands after the first read is a migration that did not
    land: order is the property, so it is asserted rather than described."""
    from cfactory import app as app_mod, cards as cards_mod, db as db_mod

    calls: list[str] = []
    monkeypatch.setattr(db_mod, "bootstrap_schema", lambda *a, **k: calls.append("bootstrap"))
    monkeypatch.setattr(app_mod, "bootstrap_schema", lambda *a, **k: calls.append("bootstrap"))
    real_get_cards_store = cards_mod.get_cards_store

    def spy(settings=None):
        calls.append("cards_store")
        return real_get_cards_store(settings)

    monkeypatch.setattr(app_mod, "get_cards_store", spy)
    monkeypatch.setenv("CFACTORY_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")

    from fastapi.testclient import TestClient

    with TestClient(app_mod.create_app()):
        pass

    assert calls[:2] == ["bootstrap", "cards_store"], calls


def test_the_migrations_are_baked_into_the_image():
    """`alembic upgrade` at startup needs alembic.ini and the revisions IN the
    image. Both sit under `apps/backend/`, which the Dockerfile copies whole and
    .dockerignore does not exclude -- asserted so a future ignore rule cannot
    quietly remove the thing that makes migrations reach production."""
    from pathlib import Path

    import cfactory

    backend = Path(cfactory.__file__).resolve().parent.parent
    repo = backend.parent.parent
    assert (backend / "alembic.ini").is_file()
    assert list((backend / "migrations" / "versions").glob("*.py"))

    ignored = {
        line.strip()
        for line in (repo / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert not {"apps/backend", "apps", "**/migrations", "*.ini"} & ignored

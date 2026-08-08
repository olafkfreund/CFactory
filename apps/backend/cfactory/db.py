"""Database plumbing: declarative base, URL resolution, engine factory, and the
one place the schema is brought under Alembic control.

CFactory uses PostgreSQL in real deployments (reusing the family's data layer)
and falls back to a local SQLite file for dev / hermetic tests. SQLAlchemy's
JSON column type works on both, so the same models run everywhere.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

from .config import Settings, get_settings

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine.interfaces import Dialect

logger = logging.getLogger(__name__)

# The revision every path below converges on. Named once so the log line, the
# stamp and the tests all quote the same string.
HEAD = "head"

# Alembic's own bookkeeping table. Its ABSENCE on a populated database is the
# whole of #308: the schema exists, nothing records which revision made it, so
# `alembic upgrade` would replay every revision against tables that are already
# there and fail on the first CREATE TABLE.
VERSION_TABLE = "alembic_version"


class Base(DeclarativeBase):
    pass


def add_column_ddl(model: type[Base], name: str, tail: str, dialect: Dialect) -> str:
    """``ALTER TABLE`` body for one late column, with its TYPE read off the model.

    Both stores carry idempotent "add the column if it is missing" guards for
    columns that landed after the table did, because the deployed bootstrap is
    ``create_all``, which never ALTERs an existing table. Those guards used to
    restate each column's type as a literal string beside the model that already
    declares it, and the two disagreed (#316): the four datetime columns were
    written ``TIMESTAMP`` where the models declare ``DateTime``, which SQLAlchemy
    renders as ``DATETIME``. So one column carried a different declared type
    depending on which of the two paths made it -- ``create_all``/Alembic, or the
    guard.

    On SQLite that was inert, because ``TIMESTAMP`` and ``DATETIME`` both carry
    NUMERIC affinity, and SQLite is what is deployed. It was not inert in the two
    directions that matter. PostgreSQL, which this module's own docstring names as
    the real deployment target, has no ``DATETIME`` type at all and no
    ``TIMESTAMP``-vs-``DATETIME`` equivalence -- so whichever literal was chosen,
    one of the two paths was wrong there rather than merely differently spelled.
    And a database a guard had touched was a THIRD schema shape, matching neither
    ``create_all`` nor ``alembic upgrade head``, which puts a hole in the equality
    #308 stamps the live database on the strength of.

    Rendering from the column removes the restatement rather than correcting it:
    ``column.type.compile(dialect)`` is the same call ``create_all`` makes, so the
    two paths cannot disagree again, and it is dialect-correct by construction --
    ``DATETIME`` on SQLite, ``TIMESTAMP WITHOUT TIME ZONE`` on PostgreSQL, from
    one declaration.

    *tail* is what the model genuinely does not carry: the nullability and
    backfill ``DEFAULT`` that exist only because this is an ALTER against a table
    that already has rows. ``create_all`` needs neither, so neither is drift.
    """
    return f"{name} {model.__table__.c[name].type.compile(dialect)} {tail}".strip()


def resolve_database_url(settings: Settings | None = None) -> str:
    """Return the configured DATABASE_URL, or a SQLite file under the workspace."""
    settings = settings or get_settings()
    if settings.database_url:
        return settings.database_url
    workspace = Path(os.path.expanduser(settings.workspace_root))
    workspace.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{workspace / 'cfactory.db'}"


def make_engine(url: str | None = None) -> Engine:
    url = url or resolve_database_url()
    is_sqlite = url.startswith("sqlite")
    # check_same_thread=False lets FastAPI's threadpool share a SQLite connection;
    # timeout makes the driver wait for a lock instead of erroring immediately.
    connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args, future=True)

    if is_sqlite:
        # SQLite defaults to a whole-database rollback-journal lock, so a read
        # (e.g. GET /api/workitems) that collides with the poll loop's write
        # fails with "database is locked" → HTTP 500. WAL lets readers run
        # concurrently with a single writer; busy_timeout makes any remaining
        # contention wait rather than raise. Applied on every new connection.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


# ── bringing the schema under Alembic control (#308) ─────────────────────────


def alembic_config(url: str) -> Config:
    """The repo's alembic.ini, pointed at *url*.

    The url travels in ``attributes`` rather than only in ``sqlalchemy.url``
    because ``migrations/env.py`` overwrites that option with its own resolution;
    the attribute is how a caller says "this database, not the resolved one",
    which is what lets a test run against a temp file.
    """
    # Imported here, not at module scope: alembic is a startup and CLI
    # dependency, and every module that touches the ORM imports this one.
    from alembic.config import Config  # noqa: PLC0415

    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["cfactory_url"] = url
    return config


def bootstrap_schema(url: str | None = None) -> str:
    """Bring the database at *url* to the head revision. Returns what it did.

    Three cases, and the middle one is #308:

    * ``"created"`` — no tables at all. Every revision runs, so a fresh
      deployment's schema is built BY the migrations rather than beside them.
    * ``"adopted"`` — tables, but no ``alembic_version``. This is every database
      this service has ever created: the schema came from
      ``Base.metadata.create_all`` at store init and nothing recorded a revision,
      so a future migration would have been written, merged, and silently never
      applied. It is STAMPED at head rather than upgraded — the tables are
      already there, and replaying the revisions that created them would fail on
      the first ``CREATE TABLE``. That the stamp is honest is not assumed: the
      schema ``create_all`` produces and the schema ``alembic upgrade head``
      produces are asserted identical by
      ``tests/test_schema_bootstrap.py::test_the_migrations_and_the_models_agree``,
      which is what makes head the right revision to stamp.
    * ``"upgraded"`` — already under Alembic control. The normal path from the
      first deploy of this change onward, and a no-op when nothing is pending.

    Called at the TOP of the app lifespan, before any store is constructed, so a
    revision lands before the code that depends on it reads the table. The
    stores' own ``create_all`` still runs afterwards and is now a no-op on a
    database this function created.

    ponytail: in-process at startup rather than an init container or a Job. The
    deployment is one replica with a Recreate strategy over a single SQLite file
    on an RWO volume, so there is no second process to race, and an init
    container would need the code, the volume and a change in another repo
    (factory-gitops) to say the same thing. If CFactory ever runs more than one
    replica this needs an advisory lock or a pre-install Job — see #308.
    """
    from alembic import command  # noqa: PLC0415

    url = url or resolve_database_url()
    engine = make_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    config = alembic_config(url)
    if VERSION_TABLE in tables:
        command.upgrade(config, HEAD)
        return "upgraded"
    if tables:
        logger.info(
            "schema exists with no %s table: stamping it at %s so future migrations apply "
            "(#308). No revision is replayed against the existing tables.",
            VERSION_TABLE,
            HEAD,
        )
        command.stamp(config, HEAD)
        return "adopted"
    command.upgrade(config, HEAD)
    return "created"

"""Database plumbing: declarative base, URL resolution, engine factory.

CFactory uses PostgreSQL in real deployments (reusing the family's data layer)
and falls back to a local SQLite file for dev / hermetic tests. SQLAlchemy's
JSON column type works on both, so the same models run everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

from .config import Settings, get_settings


class Base(DeclarativeBase):
    pass


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

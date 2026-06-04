"""Database plumbing: declarative base, URL resolution, engine factory.

CFactory uses PostgreSQL in real deployments (reusing the family's data layer)
and falls back to a local SQLite file for dev / hermetic tests. SQLAlchemy's
JSON column type works on both, so the same models run everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
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
    # check_same_thread=False lets FastAPI's threadpool share a SQLite connection.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)

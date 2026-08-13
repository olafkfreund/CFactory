"""Alembic environment for CFactory.

Resolves the database URL the same way the app does (CFACTORY_DATABASE_URL, else
a local SQLite file) and targets the cfactory metadata. Importing cfactory.store
registers the ORM models on Base.metadata so autogenerate sees them.
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

import cfactory.audit  # registers AuditEntry on Base.metadata
import cfactory.cards  # registers CardRow on Base.metadata
import cfactory.store  # noqa: F401  (registers WorkItemRow on Base.metadata)
from alembic import context
from cfactory.db import Base, resolve_database_url
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None and not logging.getLogger().handlers:
    # Only configure logging when NOTHING else has (i.e. `alembic upgrade` run
    # as its own process, where root has no handlers yet).
    #
    # #276 / Factory#923 / AIFactory#844. This call had BOTH halves of the bug:
    #
    #   1. fileConfig defaults to disable_existing_loggers=True, which silences
    #      every logger object that existed when this module imports.
    #   2. Even with that off, fileConfig still rewrites the ROOT logger from
    #      alembic.ini's [logger_root] -- level=WARNING, handlers=console.
    #
    # App loggers own no handler and propagate to root, so an in-process
    # migration cost this service every INFO record for the life of the
    # process. Only ERROR cleared the bar, so a component that ran perfectly
    # logged identically to one that never started -- the ambiguity that made
    # AIFactory#844's intake poller "appear never to start".
    #
    # Guarding on root having no handlers keeps standalone `alembic upgrade`
    # logging exactly as before, and makes the in-process path inherit the
    # app's handlers instead of destroying them.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Inject the resolved URL so alembic.ini needn't hardcode credentials. An
# explicit `cfactory_url` attribute wins, which is how `db.bootstrap_schema`
# names the database it was handed instead of the one settings resolve to (#308)
# -- a standalone `alembic upgrade` passes no attribute and resolves as before.
config.set_main_option(
    "sqlalchemy.url", config.attributes.get("cfactory_url") or resolve_database_url()
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

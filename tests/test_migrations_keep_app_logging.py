"""An in-process migration must not take over the app's logging (#276).

`migrations/env.py` called the stock `fileConfig(config.config_file_name)`,
which carries both halves of the defect:

1. `disable_existing_loggers` defaults to True, so every logger already created
   in the process gets `.disabled = True` and stays that way. That is the half
   #276 reported: one in-process `command.upgrade` in the test suite poisons
   every `logging.getLogger(__name__)` in `cfactory/` for the rest of the
   session.
2. Even with that off, `fileConfig` still rewrites the ROOT logger from
   `alembic.ini`'s `[logger_root]` -- `level = WARNING, handlers = console`.
   App loggers own no handler and propagate to root, so INFO records are
   dropped regardless of whether their logger object survived.

Only ERROR cleared the bar, which is why this is worth a test: a component that
ran perfectly logged identically to one that never started. AIFactory#844 hit
exactly that -- its intake poller "appeared never to start" because its own
startup log could not be emitted.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import cfactory
import pytest
from alembic import command
from alembic.config import Config
from cfactory import db as db_module
from cfactory.config import Settings


def _alembic_ini() -> Path:
    return Path(cfactory.__file__).resolve().parent.parent / "alembic.ini"


@pytest.fixture
def app_owned_root_logger():
    """Stand in for the app's logging setup: it clears and owns the root logger."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(logging.StreamHandler(sys.stdout))
    try:
        yield root
    finally:
        root.handlers.clear()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def test_in_process_migration_leaves_app_logging_intact(
    app_owned_root_logger, tmp_path, monkeypatch
) -> None:
    """Run a real `alembic upgrade head` the way the suite already does."""
    url = f"sqlite:///{tmp_path / 'logging.db'}"
    monkeypatch.setattr(db_module, "get_settings", lambda: Settings(database_url=url))

    app_logger = logging.getLogger("cfactory.probe_276")
    command.upgrade(Config(str(_alembic_ini())), "head")

    assert app_logger.disabled is False, (
        "alembic disabled existing loggers; every cfactory logger is now mute "
        "for the rest of this process"
    )
    assert app_owned_root_logger.level == logging.INFO, (
        "alembic reset the root logger's level; every app INFO record is now "
        "dropped for the life of this process"
    )
    assert app_owned_root_logger.handlers, "alembic replaced the app's handlers"


def test_alembic_ini_still_configures_a_standalone_run() -> None:
    """The CLI path must keep working -- the guard is 'already configured', not 'never'.

    `alembic upgrade` run as its own process has no handlers on the root, so
    alembic.ini still applies there. This asserts the ini keeps the config that
    path depends on, so a future 'just delete [logger_root]' fix does not
    silently make standalone runs mute.
    """
    assert "[logger_root]" in _alembic_ini().read_text(encoding="utf-8")


def test_env_py_guards_the_fileconfig_call() -> None:
    """The guard itself, so removing it fails here rather than in production."""
    env_py = _alembic_ini().parent / "migrations" / "env.py"
    source = env_py.read_text(encoding="utf-8")
    assert "not logging.getLogger().handlers" in source, (
        "env.py must only call fileConfig when nothing else has configured logging; see #276"
    )
    assert "disable_existing_loggers=False" in source

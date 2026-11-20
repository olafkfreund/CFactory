"""Add git_repository.investigate_test_command (#561).

Revision ID: 9c2f6a41d8e5
Revises: 3b9d5e07a1c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c2f6a41d8e5"
down_revision: str | Sequence[str] | None = "3b9d5e07a1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these by name; listing them keeps CodeQL from flagging them unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    with op.batch_alter_table("git_repository") as batch:
        batch.add_column(sa.Column("investigate_test_command", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("git_repository") as batch:
        batch.drop_column("investigate_test_command")

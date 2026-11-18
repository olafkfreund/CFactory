"""Add work-item connection and project for mixed tenants.

Revision ID: 7e4c2a91d6b8
Revises: b8d47e1f2a63
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e4c2a91d6b8"
down_revision: str | Sequence[str] | None = "b8d47e1f2a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these by name; listing them keeps CodeQL from flagging them unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    with op.batch_alter_table("git_connection") as batch:
        batch.add_column(sa.Column("work_item_connection_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("git_repository") as batch:
        batch.add_column(sa.Column("work_item_project", sa.String(length=256), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("git_repository") as batch:
        batch.drop_column("work_item_project")
    with op.batch_alter_table("git_connection") as batch:
        batch.drop_column("work_item_connection_id")

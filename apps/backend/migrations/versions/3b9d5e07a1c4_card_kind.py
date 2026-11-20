"""Add cards.kind: work or investigation (#561).

Revision ID: 3b9d5e07a1c4
Revises: 7e4c2a91d6b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b9d5e07a1c4"
down_revision: str | Sequence[str] | None = "7e4c2a91d6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these by name; listing them keeps CodeQL from flagging them unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    with op.batch_alter_table("cards") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="work")
        )


def downgrade() -> None:
    with op.batch_alter_table("cards") as batch:
        batch.drop_column("kind")

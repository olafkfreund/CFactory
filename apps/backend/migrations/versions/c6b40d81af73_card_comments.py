"""card comments + per-card completeness marker (Factory#375)

An imported card carried the issue's body but none of its discussion, which for
planning is usually where the decision actually lives. This adds the two pieces
that let the board hold a thread:

* ``card_comments`` — one row per imported comment, UNIQUE on
  ``(tenant_id, card_key, comment_id)`` where ``comment_id`` is the PROVIDER's
  own id. That index is the idempotency guard: re-importing an edited comment
  updates the row it already has and can never produce a second copy, exactly as
  ``(tenant_id, issue_ref)`` guards the cards themselves. Enforced by the
  database rather than by an application-level check, because the check loses a
  race between two concurrent polls and the constraint does not.

* ``cards.comments_synced_at`` — when that card's thread was last read IN FULL
  and successfully. NULL is load-bearing: without it, an issue with no
  discussion and an issue whose discussion failed to download are the same zero
  rows, and the board would present the second as the first. NULL on every
  existing card is therefore correct and needs no backfill — it says "never
  read", which is true, so the next poll backfills instead of claiming an empty
  thread is complete.

Revision ID: c6b40d81af73
Revises: b7e21d40c9a5
Create Date: 2026-07-26 17:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6b40d81af73"
down_revision: str | Sequence[str] | None = "b7e21d40c9a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "card_comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), server_default="default", nullable=False),
        sa.Column("card_key", sa.String(length=128), nullable=False),
        sa.Column("comment_id", sa.String(length=128), nullable=False),
        sa.Column("author", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_card_comments_tenant_card", "card_comments", ["tenant_id", "card_key"])
    op.create_index(
        "ix_card_comments_tenant_card_comment",
        "card_comments",
        ["tenant_id", "card_key", "comment_id"],
        unique=True,
    )
    with op.batch_alter_table("cards") as batch_op:
        batch_op.add_column(sa.Column("comments_synced_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("cards") as batch_op:
        batch_op.drop_column("comments_synced_at")
    op.drop_index("ix_card_comments_tenant_card_comment", table_name="card_comments")
    op.drop_index("ix_card_comments_tenant_card", table_name="card_comments")
    op.drop_table("card_comments")

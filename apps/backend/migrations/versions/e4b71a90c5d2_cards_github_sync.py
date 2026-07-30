"""cards GitHub sync columns (RFC-0019 Phase 6, §3.5, #302)

The card's mirror of its GitHub issue. ``issue_ref`` ("owner/repo#123") is both
the join and the idempotency guard — non-NULL means "already has an issue", so a
second sync adopts instead of opening a duplicate, exactly as a non-NULL
``correlation_key`` means "already in the factory".

``issue_state`` and ``labels`` are mirrored FROM GitHub and never pushed to it:
GitHub is the record of truth (RFC-0003), so on conflict its values overwrite the
card's. ``github_sync_error`` holds the last failure so a GitHub outage leaves a
truthful, visibly-stale card rather than a silent one.

Every column is nullable or defaulted, so backfilling an existing board is a
no-op: a pre-Phase-6 card simply has no issue.

Revision ID: e4b71a90c5d2
Revises: c8f41b6d0a37
Create Date: 2026-07-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b71a90c5d2"
down_revision: str | Sequence[str] | None = "c8f41b6d0a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("cards") as batch_op:
        batch_op.add_column(sa.Column("issue_ref", sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column("issue_state", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("labels", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("github_sync_error", sa.String(length=512), nullable=True))
    op.create_index(op.f("ix_cards_issue_ref"), "cards", ["issue_ref"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_cards_issue_ref"), table_name="cards")
    with op.batch_alter_table("cards") as batch_op:
        batch_op.drop_column("github_sync_error")
        batch_op.drop_column("labels")
        batch_op.drop_column("issue_state")
        batch_op.drop_column("issue_ref")

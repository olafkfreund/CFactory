"""create cards (RFC-0019 Phase 1, #302)

The planning board's own table. Deliberately NOT columns on ``work_items``:
``store.py``'s reconcile/prune machinery deletes and rewrites work-item rows
from upstream polling, which would destroy human-authored planning fields.

Mirrors the work_items conventions — ``tenant_id`` String(64) NOT NULL with a
'default' server default plus its index, and an index on ``correlation_key``
(the nullable join back to work_items, set when a card enters the factory).
``card_key`` is unique PER TENANT via a composite unique index, not globally.

Revision ID: c8f41b6d0a37
Revises: a7c3f2e19b40
Create Date: 2026-07-25 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f41b6d0a37"
down_revision: str | Sequence[str] | None = "a7c3f2e19b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "cards",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("card_key", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="backlog"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tier", sa.String(length=16), nullable=True),
        sa.Column("assignee", sa.String(length=128), nullable=True),
        sa.Column("milestone", sa.String(length=128), nullable=True),
        sa.Column("correlation_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cards_tenant_id"), "cards", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_cards_correlation_key"), "cards", ["correlation_key"], unique=False)
    op.create_index("ix_cards_tenant_id_card_key", "cards", ["tenant_id", "card_key"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_cards_tenant_id_card_key", table_name="cards")
    op.drop_index(op.f("ix_cards_correlation_key"), table_name="cards")
    op.drop_index(op.f("ix_cards_tenant_id"), table_name="cards")
    op.drop_table("cards")

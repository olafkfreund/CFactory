"""work_items tenant_id (#172)

Adds the tenant partition column. NOT NULL with a server default of 'default'
backfills every existing row to the single implicit tenant, so single-tenant
deployments are unaffected.

Revision ID: a7c3f2e19b40
Revises: 557faa62dcdc
Create Date: 2026-07-17 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c3f2e19b40"
down_revision: Union[str, Sequence[str], None] = "557faa62dcdc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("work_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default")
        )
        batch_op.create_index(batch_op.f("ix_work_items_tenant_id"), ["tenant_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("work_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_work_items_tenant_id"))
        batch_op.drop_column("tenant_id")

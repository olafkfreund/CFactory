"""card import poll lease + last polled at (#374)

Two columns on ``card_import_state``, one per thing the self-running poll needs:

* ``poll_leased_until`` — the advisory, expiring lease that decides which replica
  owns a project's cycle. The deployment runs more than one replica at times, and
  while two pollers cannot duplicate a card (the unique ``(tenant_id, issue_ref)``
  index sees to that) they can double every provider call, which is the one thing
  a background poll must not do.
* ``last_polled_at`` — when the provider was last read SUCCESSFULLY, which is a
  different question from ``last_synced_at``. That column holds the incremental
  cursor (the newest issue ``updated_at`` seen, minus an overlap), so on a
  repository whose issues have not changed in a month it stays a month old however
  often the poll runs. The cockpit's "is this board current?" needs the poll time,
  not the cursor, or every quiet repository reads as stale.

Both NULL on every existing row, which is what they mean — never leased, never
polled — so the upgrade needs no backfill.

Revision ID: b7e21d40c9a5
Revises: a1c9e4f60b72
Create Date: 2026-07-26 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e21d40c9a5"
down_revision: str | Sequence[str] | None = "a1c9e4f60b72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("card_import_state") as batch_op:
        batch_op.add_column(sa.Column("poll_leased_until", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_polled_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("card_import_state") as batch_op:
        batch_op.drop_column("last_polled_at")
        batch_op.drop_column("poll_leased_until")

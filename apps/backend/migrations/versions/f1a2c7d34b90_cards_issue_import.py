"""cards issue import: description, soft delete, unique issue_ref, watermark
(RFC-0020 §3.6, #368)

Four changes, one per property the import needs:

* ``description`` — where an imported issue's BODY lands. Mirrored (the host owns
  it, like the title). Deliberately not ``acceptance_criteria``: parsing prose
  into testable statements would fabricate what the factory verifies against.
* ``deleted_at`` — a card delete becomes a soft delete. Deleting a card means
  "not on my board", never "destroy the record of truth", and the tombstone is
  what stops the next import resurrecting it.
* UNIQUE ``(tenant_id, issue_ref)`` — import idempotency enforced by the
  DATABASE. An application-level "does this card exist?" check loses a race
  between two concurrent polls; a unique constraint does not.
* ``card_import_state`` — the ``last_synced_at`` watermark per (tenant, project)
  that the incremental pass asks the provider for.

Revision ID: f1a2c7d34b90
Revises: e4b71a90c5d2
Create Date: 2026-07-25 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2c7d34b90"
down_revision: str | Sequence[str] | None = "e4b71a90c5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("cards") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
    # A board that already holds two cards pointing at ONE issue (nothing forbade
    # it before this index) must merge them before this migration will apply.
    op.create_index(
        "ix_cards_tenant_id_issue_ref", "cards", ["tenant_id", "issue_ref"], unique=True
    )
    op.create_table(
        "card_import_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("project", sa.String(length=256), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_card_import_state_tenant_project",
        "card_import_state",
        ["tenant_id", "project"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_card_import_state_tenant_project", table_name="card_import_state")
    op.drop_table("card_import_state")
    op.drop_index("ix_cards_tenant_id_issue_ref", table_name="cards")
    with op.batch_alter_table("cards") as batch_op:
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("description")

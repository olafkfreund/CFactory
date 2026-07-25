"""cards per-stage dispatch record (RFC-0020 Phase 7, §3.7, #369)

``stage_runs`` is the per-stage dispatch record the explicit plan / code / test
stage actions are idempotent on: ``{"<stage>": {"service", "status",
"dispatched_at", "ref", "detail"}}``.

It has to exist for a sequence to be possible at all. Before Phase 7 the
idempotency guard was "``correlation_key`` is non-NULL means already in the
factory" — but planning SETS that key, so every stage after the first saw a
non-NULL key and no-oped, and a plan -> code -> test run stopped dead after the
plan. The key now means "the key this card's work is threaded on, reuse it" and
this column answers the narrower question the guard actually needed to ask: has
THIS stage already been dispatched.

Defaulted, so backfilling an existing board is a no-op: a pre-Phase-7 card has
no stage runs, and its implicit tier dispatch keeps behaving exactly as before.

Revision ID: f9a5c07d21b3
Revises: e4b71a90c5d2
Create Date: 2026-07-25 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9a5c07d21b3"
down_revision: str | Sequence[str] | None = "e4b71a90c5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("cards") as batch_op:
        batch_op.add_column(
            sa.Column("stage_runs", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("cards") as batch_op:
        batch_op.drop_column("stage_runs")

"""tenant git configuration (RFC-0020 §3.3, #363)

One row per tenant holding which git host the board syncs with, which project,
which project issues are imported from, and which AIFactory project a dispatched
card is built in — replacing the process-global ``CFACTORY_INTAKE_PROJECT_ID`` /
``CFACTORY_GITHUB_*`` environment variables with a resource the cockpit can edit.

No credential column, and deliberately so: credential custody is RFC-0020 §3.4
(phases 3 and 4), and the derived ``credential_missing`` status is what reports a
tenant that has named a project the deployment has no usable token for.

Nothing is backfilled here. The default tenant's row is materialised at boot from
the legacy environment variables (``CardStore.seed_git_config_from_env``), which
is the one-release bridge — a migration cannot see the process environment of the
deployment it will run in.

Also merges the two heads this repository grew when the §3.6 import columns and
the §3.7 stage-run column were revised from the same parent in parallel, so
``alembic upgrade head`` resolves again.

Revision ID: b3d92f47c810
Revises: f1a2c7d34b90, f9a5c07d21b3
Create Date: 2026-07-26 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d92f47c810"
down_revision: str | Sequence[str] | None = ("f1a2c7d34b90", "f9a5c07d21b3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenant_git_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="github"),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("project", sa.String(length=256), nullable=True),
        sa.Column("intake_project", sa.String(length=256), nullable=True),
        sa.Column("aifactory_project_id", sa.String(length=128), nullable=True),
        sa.Column("default_labels", sa.JSON(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("verify_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    # Exactly one configuration per tenant, enforced by the database: two
    # concurrent first-ever writes both find no row and both insert, and the
    # application-level check loses that race where the constraint does not.
    op.create_index(
        "ix_tenant_git_config_tenant", "tenant_git_config", ["tenant_id"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tenant_git_config_tenant", table_name="tenant_git_config")
    op.drop_table("tenant_git_config")

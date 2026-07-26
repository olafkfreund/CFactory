"""encrypted tenant git credential (RFC-0020 §3.4, #364)

One sealed credential per tenant, plus the column on ``tenant_git_config`` that
records a credential the HOST refused.

**Envelope encryption, and what each column holds.** ``ciphertext`` is the
credential sealed with a per-record AES-GCM data key; ``wrapped_key`` is that
data key sealed with the deployment's key-encryption key
(``CFACTORY_CREDENTIAL_KEY``); ``key_version`` names which KEK did the wrapping.
Both blobs are ``nonce || ciphertext||tag``. There is no plaintext column and
there is no nullable "unencrypted fallback" — a deployment with no key refuses to
store a credential rather than storing one in the clear.

``key_version`` is what makes a KEK rotation — or a later move to a KMS-held KEK
(Factory#314/#315) — a re-wrap of this row rather than a change to this schema.

Nothing is backfilled. The deployment's ``CFACTORY_GIT_PROVIDER_TOKEN`` remains
the fallback for every tenant that has not stored one, so an existing
single-tenant deploy keeps working with no operator action; a migration could not
seed these rows anyway, since it has no key to encrypt with.

Revision ID: d5e83a1c9f22
Revises: b3d92f47c810
Create Date: 2026-07-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e83a1c9f22"
down_revision: str | Sequence[str] | None = "b3d92f47c810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenant_git_credential",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("key_version", sa.String(length=32), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    # Exactly one credential per tenant, enforced by the database for the same
    # reason the configuration's is: two concurrent first-ever writes both find
    # no row and both insert, and an application-level check loses that race.
    op.create_index(
        "ix_tenant_git_credential_tenant", "tenant_git_credential", ["tenant_id"], unique=True
    )
    # Nullable and unset: "nothing has been proved either way about this
    # tenant's credential", which is true of every existing row.
    op.add_column(
        "tenant_git_config", sa.Column("credential_rejected", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropping the table DESTROYS every stored credential — they exist nowhere
    else, and nothing can reconstruct them. Tenants fall back to the deployment's
    environment credential and report ``credential_missing`` where there is none.
    """
    op.drop_column("tenant_git_config", "credential_rejected")
    op.drop_index("ix_tenant_git_credential_tenant", table_name="tenant_git_credential")
    op.drop_table("tenant_git_credential")

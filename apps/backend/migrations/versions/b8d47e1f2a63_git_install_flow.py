"""GitHub App / GitLab OAuth install flow (RFC-0020 §3.4 phase 4, #365)

Two new tables, and NOTHING is moved into them: this migration is additive, so a
deployment that upgrades and never runs an install keeps behaving exactly as it
did on the phase-3 paste-box path.

* ``git_install`` — one row per connection, recording HOW that connection is
  authenticated when it was authenticated by an install: the GitHub
  ``installation_id`` (an identifier GitHub prints in its own URLs — not a
  secret), the account it landed on, and the status plus the last failure reason.
  A refresh that fails writes ``credential_missing`` here, which is what makes the
  degradation visible in the Settings panel rather than only at the next board
  write.
* ``git_install_state`` — a PENDING install. It exists so the callback can decide
  whether to believe a redirect: it stores the SHA-256 of a 256-bit state token
  (never the token), the tenant and connection the install was started for, the
  redirect URI that was sent, and an expiry. The application deletes the row when
  it consumes it, which is what makes a replayed callback URL match nothing.

**No credential column anywhere here.** The GitHub half stores no secret at all —
its long-lived one is the App private key, which is deployment configuration, and
its short-lived ones are minted per call and never written down. The GitLab half
stores a refresh token, and it stores it in ``tenant_git_credential``, encrypted
by the phase-3 envelope exactly like a pasted PAT. There is no second credential
table and no second crypto path.

Revision ID: b8d47e1f2a63
Revises: c6b40d81af73
Create Date: 2026-07-26 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d47e1f2a63"
down_revision: str | Sequence[str] | None = "c6b40d81af73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "git_install",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="github"),
        # GitHub only; NULL on GitLab, whose equivalent state is the sealed
        # refresh token in tenant_git_credential.
        sa.Column("installation_id", sa.String(length=64), nullable=True),
        sa.Column("account", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="installed"),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_git_install_tenant_id", "git_install", ["tenant_id"])
    # One install per connection, enforced by the database because the
    # application check loses the race between two concurrent callbacks.
    op.create_index("ix_git_install_connection", "git_install", ["connection_id"], unique=True)

    op.create_table(
        "git_install_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # SHA-256 hex of the state token. NOT the token: a dump, a replica or a
        # backup must not hand anybody a state they can present.
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="github"),
        sa.Column("redirect_uri", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_git_install_state_hash", "git_install_state", ["state_hash"], unique=True)


def downgrade() -> None:
    """Downgrade schema.

    Both tables go. A tenant that had authenticated a connection by installing
    reads as ``credential_missing`` on the older code and has to store a
    credential the phase-3 way — which is the honest outcome, since the older code
    has no way to mint an installation token. Nothing leaks: the only secret the
    flow ever stored is the GitLab refresh token, which lives in
    ``tenant_git_credential`` and is untouched here.
    """
    op.drop_index("ix_git_install_state_hash", table_name="git_install_state")
    op.drop_table("git_install_state")
    op.drop_index("ix_git_install_connection", table_name="git_install")
    op.drop_index("ix_git_install_tenant_id", table_name="git_install")
    op.drop_table("git_install")

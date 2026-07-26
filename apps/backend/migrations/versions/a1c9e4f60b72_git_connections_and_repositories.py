"""multiple git connections and repositories per tenant (RFC-0020 §3.3 phase 8, #373)

Splits the one-row-per-tenant ``tenant_git_config`` into two levels:

* ``git_connection`` — a provider + a host + a credential + a label + whatever the
  last verify proved. MANY per tenant, unique on (tenant_id, provider, base_url)
  so one host is never configured twice. ``base_url`` is NOT NULL with ``''``
  meaning "the provider's public default": NULLs are distinct in both SQLite and
  PostgreSQL, so a nullable column would let two rows both claim the default host.
* ``git_repository`` — a project path, the project issues are imported from, the
  AIFactory project builds land in, and the labels the board puts on issues it
  opens. MANY per connection, unique on (connection_id, project).

**Exactly one repository per tenant is the default, and the database enforces it.**
``default_for_tenant`` holds the tenant id when the row is that tenant's default and
NULL otherwise, with a UNIQUE index on it — so any number of non-defaults are
accepted and a second default for one tenant is refused. A nullable unique column
rather than a boolean plus a partial index, because a partial index is
dialect-specific (``sqlite_where`` / ``postgresql_where``) and would silently
degrade into a full unique index — which would forbid two non-default repositories.

The credential moves from the tenant to the CONNECTION: ``tenant_git_credential``
gains ``connection_id`` (unique, so one credential per connection) and
``aad_version``, and loses its per-tenant unique index — one credential per tenant
is precisely the limitation being removed.

**What is migrated here, and what is deliberately NOT.** Every existing
configuration row becomes one connection plus, when it named a project, one
repository marked as that tenant's default, preserving provider, base_url,
project, intake_project, aifactory_project_id, default_labels and the verify
state. The existing credential row is attached to its tenant's new connection and
marked ``aad_version = 1`` — the pre-phase-8 binding, which is what it currently
is.

**The credential is NOT re-sealed here.** Its associated data must now bind the
connection, and re-sealing means decrypting and re-encrypting, which needs
``CFACTORY_CREDENTIAL_KEY``. A migration process is not guaranteed to hold that
key (and this deployment may bootstrap with ``create_all`` and never run Alembic
at all), so doing it here would either fail the upgrade or silently skip the
credential. It is done in the application instead —
``CardStore.adopt_legacy_git_config`` at boot, and lazily on the first read —
where the key is present by definition, in memory, never written to disk or logs.
A record that is still ``aad_version = 1`` keeps working: the read path uses the
legacy binding for exactly those rows.

``tenant_git_config`` is left in place, unread from here on. It is the source this
migration adopts from and the thing a downgrade needs, and dropping a table whose
data has just been copied is how an upgrade becomes unrecoverable.

Revision ID: a1c9e4f60b72
Revises: d5e83a1c9f22
Create Date: 2026-07-26 16:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c9e4f60b72"
down_revision: str | Sequence[str] | None = "d5e83a1c9f22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The pre-phase-8 associated-data binding: tenant only, no connection. Every
# adopted credential starts here and is re-sealed by the application.
_LEGACY_AAD_VERSION = 1


def _connection_table() -> sa.Table:
    """A minimal handle on ``git_connection`` for the data copy.

    Declared rather than reflected so the copy does not depend on what a
    particular database happens to have, and typed so the JSON column is
    serialised the way each dialect wants it.
    """
    return sa.table(
        "git_connection",
        sa.column("tenant_id", sa.String),
        sa.column("provider", sa.String),
        sa.column("base_url", sa.String),
        sa.column("label", sa.String),
        sa.column("verified_at", sa.DateTime),
        sa.column("verify_error", sa.String),
        sa.column("credential_rejected", sa.Boolean),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )


def _repository_table() -> sa.Table:
    return sa.table(
        "git_repository",
        sa.column("tenant_id", sa.String),
        sa.column("connection_id", sa.Integer),
        sa.column("project", sa.String),
        sa.column("intake_project", sa.String),
        sa.column("aifactory_project_id", sa.String),
        sa.column("default_labels", sa.JSON),
        sa.column("default_for_tenant", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "git_connection",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="github"),
        # '' = the provider's public default. See the module docstring for why not
        # NULL.
        sa.Column("base_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("verify_error", sa.String(length=512), nullable=True),
        sa.Column("credential_rejected", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_git_connection_tenant_id", "git_connection", ["tenant_id"])
    # One host per tenant, configured once — enforced by the database because the
    # application check loses the race between two concurrent creates.
    op.create_index(
        "ix_git_connection_tenant_provider_base",
        "git_connection",
        ["tenant_id", "provider", "base_url"],
        unique=True,
    )

    op.create_table(
        "git_repository",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("project", sa.String(length=256), nullable=False),
        sa.Column("intake_project", sa.String(length=256), nullable=True),
        sa.Column("aifactory_project_id", sa.String(length=128), nullable=True),
        sa.Column("default_labels", sa.JSON(), nullable=True),
        sa.Column("default_for_tenant", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_git_repository_tenant_id", "git_repository", ["tenant_id"])
    op.create_index("ix_git_repository_connection_id", "git_repository", ["connection_id"])
    op.create_index(
        "ix_git_repository_connection_project",
        "git_repository",
        ["connection_id", "project"],
        unique=True,
    )
    # THE SINGLE-DEFAULT CONSTRAINT (see the module docstring).
    op.create_index(
        "ix_git_repository_default", "git_repository", ["default_for_tenant"], unique=True
    )

    # The credential becomes per-connection.
    op.add_column("tenant_git_credential", sa.Column("connection_id", sa.Integer(), nullable=True))
    op.add_column(
        "tenant_git_credential",
        sa.Column(
            "aad_version",
            sa.Integer(),
            nullable=False,
            server_default=str(_LEGACY_AAD_VERSION),
        ),
    )
    op.drop_index("ix_tenant_git_credential_tenant", table_name="tenant_git_credential")
    op.create_index("ix_tenant_git_credential_tenant_id", "tenant_git_credential", ["tenant_id"])
    op.create_index(
        "ix_tenant_git_credential_connection",
        "tenant_git_credential",
        ["connection_id"],
        unique=True,
    )

    # Which repository a card is for. NULL = the tenant's default, so every
    # existing card keeps behaving exactly as it did.
    op.add_column("cards", sa.Column("repository_id", sa.Integer(), nullable=True))

    _adopt_existing_configurations()


def _adopt_existing_configurations() -> None:
    """Turn each ``tenant_git_config`` row into a connection + a default repository."""
    conn = op.get_bind()
    legacy = conn.execute(
        sa.text(
            "SELECT tenant_id, provider, base_url, project, intake_project, "
            "aifactory_project_id, default_labels, verified_at, verify_error, "
            "credential_rejected FROM tenant_git_config"
        )
    ).mappings()
    connections, repositories = _connection_table(), _repository_table()

    def labels_of(value: object) -> list[str]:
        """``default_labels`` as a list, whatever the dialect handed back.

        A textual SELECT is not typed, so SQLite returns the raw JSON text while
        PostgreSQL returns the decoded value. Writing the text straight into a JSON
        column would double-encode it, and the tenant's labels would come back as
        one string containing brackets.
        """
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else []
        return [label for label in value if isinstance(label, str)] if isinstance(value, list) else []

    for row in legacy:
        provider = (row["provider"] or "github").strip().lower()
        base_url = (row["base_url"] or "").strip().rstrip("/")
        conn.execute(
            connections.insert().values(
                tenant_id=row["tenant_id"],
                provider=provider,
                base_url=base_url,
                label=provider,
                verified_at=row["verified_at"],
                verify_error=row["verify_error"],
                credential_rejected=row["credential_rejected"],
                created_at=sa.func.current_timestamp(),
                updated_at=sa.func.current_timestamp(),
            )
        )
        # Read the id back rather than relying on a dialect-specific RETURNING or
        # lastrowid: the triple is unique, so this is exact on every backend.
        connection_id = conn.execute(
            sa.text(
                "SELECT id FROM git_connection WHERE tenant_id = :tenant "
                "AND provider = :provider AND base_url = :base_url"
            ),
            {"tenant": row["tenant_id"], "provider": provider, "base_url": base_url},
        ).scalar_one()
        if row["project"]:
            conn.execute(
                repositories.insert().values(
                    tenant_id=row["tenant_id"],
                    connection_id=connection_id,
                    project=row["project"],
                    intake_project=row["intake_project"],
                    aifactory_project_id=row["aifactory_project_id"],
                    default_labels=labels_of(row["default_labels"]),
                    # The adopted repository is the tenant's default, so every card
                    # that names none resolves exactly where it did before.
                    default_for_tenant=row["tenant_id"],
                    created_at=sa.func.current_timestamp(),
                    updated_at=sa.func.current_timestamp(),
                )
            )
        # Attach this tenant's credential to its new connection, still marked with
        # the pre-phase-8 binding. It is re-sealed by the application, which is the
        # only place that holds the encryption key.
        conn.execute(
            sa.text(
                "UPDATE tenant_git_credential SET connection_id = :connection, "
                "aad_version = :aad WHERE tenant_id = :tenant AND connection_id IS NULL"
            ),
            {
                "connection": connection_id,
                "aad": _LEGACY_AAD_VERSION,
                "tenant": row["tenant_id"],
            },
        )


def downgrade() -> None:
    """Downgrade schema.

    ``tenant_git_config`` was never dropped, so the pre-phase-8 code finds its row
    exactly where it left it — including any verify state, because the adoption
    copied rather than moved.

    **One thing does not come back on its own: a credential the application has
    already re-sealed onto the connection binding.** Pre-phase-8 code binds the
    associated data to the tenant alone, so a re-sealed record reads as
    undecryptable, the board reports ``credential_missing``, and the credential has
    to be stored again through the panel. Nothing is destroyed and nothing leaks —
    it is a re-entry, and it is the price of binding a credential to a connection
    that the older schema has no concept of. A repository or connection added after
    the upgrade is also not represented in the single legacy row; it stays in these
    tables, which is why they are dropped last and only here.
    """
    op.drop_column("cards", "repository_id")
    op.drop_index("ix_tenant_git_credential_connection", table_name="tenant_git_credential")
    op.drop_index("ix_tenant_git_credential_tenant_id", table_name="tenant_git_credential")
    op.create_index(
        "ix_tenant_git_credential_tenant", "tenant_git_credential", ["tenant_id"], unique=True
    )
    op.drop_column("tenant_git_credential", "aad_version")
    op.drop_column("tenant_git_credential", "connection_id")
    op.drop_index("ix_git_repository_default", table_name="git_repository")
    op.drop_index("ix_git_repository_connection_project", table_name="git_repository")
    op.drop_index("ix_git_repository_connection_id", table_name="git_repository")
    op.drop_index("ix_git_repository_tenant_id", table_name="git_repository")
    op.drop_table("git_repository")
    op.drop_index("ix_git_connection_tenant_provider_base", table_name="git_connection")
    op.drop_index("ix_git_connection_tenant_id", table_name="git_connection")
    op.drop_table("git_connection")

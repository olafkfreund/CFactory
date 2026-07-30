"""Many git connections, many repositories, per tenant (RFC-0020 §3.3, phase 8).

Phase 2 gave a tenant ONE git configuration: one provider, one host, one
repository, one credential. That is wrong for any real organisation — a team has
repositories on more than one host, and a planning board should be able to hold
work across several of them — so this module replaces the flat row with two
levels:

* a **connection** is a place the board can authenticate to: a provider, a host
  (``base_url``), a credential, a human label, and whatever the last verify
  proved. A tenant has as many as it likes, and no two of them may name the same
  ``(provider, base_url)`` — configuring one host twice is a mistake, not a
  feature, and it would leave "which credential reaches github.com?" ambiguous;
* a **repository** is something to work on THROUGH a connection: a project path,
  the project issues are imported from, the AIFactory project builds land in, and
  the labels the board puts on issues it opens. A connection has as many as it
  likes.

**Exactly one repository per tenant is the DEFAULT**, and that is enforced by the
database, not by a check: see ``default_for_tenant`` below. A card that names no
repository resolves to it, which is what keeps every pre-phase-8 caller — a card
created in the cockpit with no repository chosen, the background import, a sync
hook — working with no change in behaviour.

**Why the credential hangs off the connection and not the repository.** A
credential authenticates to a HOST; two repositories on the same host are reached
with the same token, and duplicating it per repository would mean rotating it in
two places and leaking it from two rows. That is also why verification is
per-connection: what a verify proves is "this host answers, this credential is
accepted", and it proves it by reading one of the connection's repositories.

The wire models here are what the Settings panel renders and what the MCP tools
return. The stored ones are read and written by :class:`cfactory.cards.CardStore`
and nowhere else, exactly as the phase-2 row was.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .config import DEFAULT_TENANT, Settings
from .credentials import Credential, CredentialInfo, env_credential
from .db import Base
from .git_config import (
    GITHUB,
    PROVIDER_DEFAULT_BASE_URL,
    GitConfigError,
    GitTarget,
    derive_status,
    provider_token,
    validate_base_url,
    validate_labels,
    validate_project,
    validate_provider,
)
from .git_install import GitInstall, GitInstallRow, install_view

# A connection label is a human name shown in the panel ("Work GitHub",
# "self-hosted GitLab"), never addressed by anything, so it is bounded and
# otherwise unvalidated.
LABEL_MAX = 128

# The sentinel stored in ``base_url`` for "the provider's public default". An
# empty string rather than NULL because the uniqueness of (tenant, provider,
# base_url) has to hold for it: NULLs are DISTINCT in both SQLite and PostgreSQL,
# so two rows with a NULL base_url would both be accepted and the tenant would
# have github.com configured twice.
DEFAULT_BASE_URL = ""


def _now() -> datetime:
    return datetime.now(UTC)


class GitResourceNotFoundError(LookupError):
    """A connection or repository this tenant does not have.

    Rendered as a 404 over REST and an error payload over MCP. Raised for a
    resource that belongs to ANOTHER tenant too — "not found" is the only honest
    answer to give a caller who may not know it exists (RFC-0020 §3.3's isolation
    rule; a 403 would confirm the id).
    """


class GitConnectionRow(Base):
    """One place a tenant's board can authenticate to."""

    __tablename__ = "git_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default=GITHUB, server_default=GITHUB)
    # "" means the provider's public default (see DEFAULT_BASE_URL).
    base_url: Mapped[str] = mapped_column(String(512), default="", server_default="")
    label: Mapped[str] = mapped_column(String(LABEL_MAX), default="", server_default="")
    # Set by a successful verify, CLEARED by any edit: a verification proves the
    # connection that was verified, so it cannot survive that connection changing.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verify_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # True when the last verify was refused by the HOST for the credential (401 /
    # 403). NULL means nothing has been proved either way. See
    # :func:`cfactory.git_config.derive_status` for why a refused credential reads
    # as ``credential_missing`` rather than as a green ``configured``.
    credential_rejected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        # One host, configured once. Enforced by the database because the
        # application check loses the race between two concurrent creates.
        Index(
            "ix_git_connection_tenant_provider_base",
            "tenant_id",
            "provider",
            "base_url",
            unique=True,
        ),
    )


class GitRepositoryRow(Base):
    """One repository a tenant works on, through exactly one connection."""

    __tablename__ = "git_repository"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Denormalised from the connection, and load-bearing twice: it is what scopes
    # a repository read without a join, and it is what makes the single-default
    # rule below a database constraint.
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT, index=True
    )
    connection_id: Mapped[int] = mapped_column(Integer, index=True)
    project: Mapped[str] = mapped_column(String(256))
    intake_project: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The AIFactory project id a dispatched card is BUILT in — an opaque UUID, not
    # a repository path. See :mod:`cfactory.git_config` for why the two are
    # separate fields.
    aifactory_project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # THE SINGLE-DEFAULT CONSTRAINT. Holds this row's ``tenant_id`` when it is the
    # tenant's default repository and NULL otherwise, with a UNIQUE index on it:
    # NULLs are distinct in both SQLite and PostgreSQL, so any number of
    # non-defaults are accepted and a second default for the same tenant is
    # REFUSED by the database.
    #
    # A nullable unique column rather than a boolean plus a partial index because
    # a partial index is dialect-specific (``sqlite_where`` / ``postgresql_where``)
    # and would silently degrade to a full unique index — which would forbid two
    # non-default repositories — on any dialect that had neither. This shape needs
    # no dialect knowledge at all. ``is_default`` is derived from it, so there is
    # one column and one source of truth.
    default_for_tenant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        # One project, added to a connection once.
        Index("ix_git_repository_connection_project", "connection_id", "project", unique=True),
        Index("ix_git_repository_default", "default_for_tenant", unique=True),
    )

    @property
    def is_default(self) -> bool:
        return self.default_for_tenant is not None


# ── wire models ──────────────────────────────────────────────────────────────


class GitRepository(BaseModel):
    """The wire view of one repository. No credential — there is none here."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    connection_id: int
    tenant_id: str
    project: str
    intake_project: str | None = None
    aifactory_project_id: str | None = None
    default_labels: list[str] = Field(default_factory=list)
    # Whether a card that names no repository resolves to this one.
    is_default: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GitConnection(BaseModel):
    """The wire view of one connection, with its repositories.

    ``credential`` is the MASKED indicator (RFC-0020 §3.4) — whether there is one,
    when it was stored, which key wraps it — and is the only thing any read
    surface ever says about it. ``status`` is derived on every read, never stored.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    provider: str
    # Always the resolved value (the tenant's own, or the provider default), so
    # the panel can show where calls actually go.
    base_url: str
    label: str
    status: str
    credential: CredentialInfo
    # The install this connection was authenticated by, when it was (RFC-0020 §3.4
    # phase 4). Present alongside ``credential`` rather than inside it because
    # they answer different questions: ``credential`` says whether the board can
    # reach the host, ``install`` says HOW — which installation, on whose account,
    # and why the last mint failed if it did. Absent for a connection holding a
    # pasted credential or running on the deployment's environment one.
    install: GitInstall | None = None
    verified_at: datetime | None = None
    verify_error: str | None = None
    repositories: list[GitRepository] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GitConnectionCreate(BaseModel):
    """POST body for a connection. No credential: that is its own write-only
    endpoint, and a create body is the kind of thing that ends up in a log."""

    provider: str = GITHUB
    base_url: str | None = Field(default=None, max_length=512)
    label: str | None = Field(default=None, max_length=LABEL_MAX)


class GitConnectionUpdate(BaseModel):
    """PATCH body for a connection: only the fields present are applied.

    A patch rather than a full replacement, because the two things most often
    edited — a label typo, a moved self-hosted host — must not require re-sending
    the rest. Changing the provider or the host CLEARS the verification, which
    proved a connection this one no longer is; changing only the label does not.
    """

    provider: str | None = None
    base_url: str | None = Field(default=None, max_length=512)
    label: str | None = Field(default=None, max_length=LABEL_MAX)


class GitRepositoryCreate(BaseModel):
    """POST body for a repository under a connection."""

    project: str = Field(min_length=1, max_length=256)
    intake_project: str | None = Field(default=None, max_length=256)
    aifactory_project_id: str | None = Field(default=None, max_length=128)
    default_labels: list[str] = Field(default_factory=list)
    # Make this the tenant's default. The FIRST repository a tenant creates
    # becomes the default whatever this says — a tenant with repositories and no
    # default would silently refuse every card that named none.
    make_default: bool = False


class GitRepositoryUpdate(BaseModel):
    """PATCH body for a repository: only the fields present are applied.

    ``intake_project`` and ``aifactory_project_id`` are explicitly nullable — a
    patch that sends ``null`` CLEARS them, which is what makes "take the intake
    project back off" expressible without a full replacement.
    """

    project: str | None = Field(default=None, min_length=1, max_length=256)
    intake_project: str | None = Field(default=None, max_length=256)
    aifactory_project_id: str | None = Field(default=None, max_length=128)
    default_labels: list[str] | None = None


# ── validation ───────────────────────────────────────────────────────────────


def validate_label(label: str | None, provider: str) -> str:
    """The human name for a connection, defaulting to the provider's own name."""
    value = (label or "").strip()
    if len(value) > LABEL_MAX:
        raise GitConfigError(f"label must be at most {LABEL_MAX} characters")
    return value or provider


def connection_fields(provider: str, base_url: str | None, label: str | None) -> dict[str, object]:
    """Validated stored column values for a connection write."""
    kind = validate_provider(provider)
    return {
        "provider": kind,
        "base_url": validate_base_url(base_url) or DEFAULT_BASE_URL,
        "label": validate_label(label, kind),
    }


def repository_fields(body: GitRepositoryCreate, provider: str) -> dict[str, object]:
    """Validated stored column values for a repository write.

    Validated against the CONNECTION's provider, because that is what has to be
    able to address the path: ``organization/project/repo`` on Azure DevOps,
    ``owner/repo`` elsewhere.
    """
    project = validate_project(body.project, provider)
    if project is None:
        raise GitConfigError("project is required: a repository is a project path")
    return {
        "project": project,
        "intake_project": validate_project(body.intake_project, provider, field="intake_project"),
        "aifactory_project_id": (body.aifactory_project_id or "").strip() or None,
        "default_labels": validate_labels(body.default_labels),
    }


def repository_patch(body: GitRepositoryUpdate, provider: str) -> dict[str, object]:
    """The validated subset of a repository patch that was actually sent."""
    sent = body.model_dump(exclude_unset=True)
    fields: dict[str, object] = {}
    if "project" in sent:
        project = validate_project(body.project, provider)
        if project is None:
            raise GitConfigError("project must not be cleared: a repository is a project path")
        fields["project"] = project
    if "intake_project" in sent:
        fields["intake_project"] = validate_project(
            body.intake_project, provider, field="intake_project"
        )
    if "aifactory_project_id" in sent:
        fields["aifactory_project_id"] = (body.aifactory_project_id or "").strip() or None
    if "default_labels" in sent:
        fields["default_labels"] = validate_labels(body.default_labels)
    return fields


# ── resolution ───────────────────────────────────────────────────────────────


def resolved_base_url(provider: str, base_url: str) -> str:
    """The host a connection actually calls."""
    return base_url or PROVIDER_DEFAULT_BASE_URL.get(provider, "")


@dataclass(frozen=True)
class ResolvedRepository:
    """A repository and the connection it is reached through.

    The pair the store hands back, so a consumer never has to re-look-up one from
    the other — and so the credential, which belongs to the connection, is
    resolved exactly once per target.
    """

    connection: GitConnectionRow
    repository: GitRepositoryRow


def target_from_repository(
    resolved: ResolvedRepository, settings: Settings, credential: Credential | None = None
) -> GitTarget:
    """The :class:`~cfactory.git_config.GitTarget` a repository describes.

    The SAME shape phase 2 produced, on purpose: ``github_sync``,
    ``issue_import``, ``card_intake`` and ``git_providers`` consume a target and
    are unchanged by this phase apart from asking for the right one. What used to
    be "the tenant's config" is now "this card's repository, or the tenant's
    default".
    """
    connection, repository = resolved.connection, resolved.repository
    provider = (connection.provider or GITHUB).strip().lower()
    return GitTarget(
        tenant=connection.tenant_id,
        provider=provider,
        base_url=resolved_base_url(provider, connection.base_url or ""),
        project=repository.project,
        import_project=repository.intake_project or repository.project,
        aifactory_project_id=repository.aifactory_project_id,
        default_labels=tuple(
            label for label in (repository.default_labels or []) if isinstance(label, str)
        ),
        credential=credential or env_credential(provider_token(settings)),
        stored=True,
        verified_at=connection.verified_at,
        verify_error=connection.verify_error,
        credential_rejected=connection.credential_rejected,
        connection_id=connection.id,
        repository_id=repository.id,
    )


def repository_view(row: GitRepositoryRow) -> GitRepository:
    return GitRepository(
        id=row.id,
        connection_id=row.connection_id,
        tenant_id=row.tenant_id,
        project=row.project,
        intake_project=row.intake_project,
        aifactory_project_id=row.aifactory_project_id,
        default_labels=[label for label in (row.default_labels or []) if isinstance(label, str)],
        is_default=row.is_default,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def connection_view(
    row: GitConnectionRow,
    repositories: Sequence[GitRepositoryRow],
    credential: CredentialInfo,
    install: GitInstallRow | None = None,
) -> GitConnection:
    """The wire model for one connection.

    ``status`` reuses the phase-2 derivation with "has a project" read as "has a
    repository": a connection with no repository is ``unconfigured``, because
    there is nothing for it to reach.

    A degraded install lands in ``credential_missing`` through the same door as a
    missing credential: ``credential.configured`` is false for any install status
    but ``installed``, so there is one status rule and not two that could
    disagree.
    """
    provider = (row.provider or GITHUB).strip().lower()
    return GitConnection(
        id=row.id,
        tenant_id=row.tenant_id,
        provider=provider,
        base_url=resolved_base_url(provider, row.base_url or ""),
        label=row.label or provider,
        status=derive_status(
            project=repositories[0].project if repositories else None,
            has_credential=credential.configured,
            verified_at=row.verified_at,
            credential_rejected=row.credential_rejected,
        ),
        credential=credential,
        install=install_view(install) if install is not None else None,
        verified_at=row.verified_at,
        verify_error=row.verify_error,
        repositories=[repository_view(repo) for repo in repositories],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )

"""Tenant-scoped git configuration (RFC-0020 §3.3).

Which git host the board talks to, and which project it syncs with, is a
**tenant-level resource** here rather than a process-global environment
variable. A tenant has exactly one git configuration, it is editable from the
cockpit, and it is the single thing every consumer reads:
:mod:`cfactory.github_sync` (open/adopt an issue), :mod:`cfactory.issue_import`
(pull a repo's backlog in) and :mod:`cfactory.card_intake` (dispatch a card into
AIFactory/TFactory) all resolve through :meth:`cfactory.cards.CardStore.git_target`
and nowhere else.

**Why this exists at all.** Before it, the two questions a user most wants to
answer — "which repo does my board sync with?" and "which project do my builds
land in?" — could only be answered by an operator with a redeploy, and one of
the two env vars (``CFACTORY_INTAKE_PROJECT_ID``) was an opaque UUID with no
explanation anywhere in the portal.

**The environment is a SEED, not a source of truth.** On first boot
:meth:`cfactory.cards.CardStore.seed_git_config_from_env` materialises the
default tenant's row from the legacy env vars, exactly once; from then on the
stored row is authoritative and editable, and the env vars are ignored for that
tenant. A tenant that has no row yet still *resolves* against the env values, so
a deploy that never boots the seed — or a unit test that never writes a row —
behaves precisely as it did before this module existed. That fallback is the
one-release bridge; when the env vars are removed, resolution simply returns
``unconfigured`` until somebody fills the panel in.

**Credentials are deliberately absent.** Phases 3 and 4 of RFC-0020 own
credential storage and OAuth; the token still comes from the environment
(:func:`provider_token`). That is what the derived ``credential_missing`` status
is *for*: the tenant has said which project it wants, and no usable credential is
present to reach it. This follows the copilot-settings precedent exactly —
provider and model are persisted, the API key never is.

**Two intake fields, not one.** RFC-0020 §3.3 lists one ``intake_project``,
described as "where intake files if different from the sync target". In this
codebase those are two different things in two different namespaces:

* ``intake_project`` — a git project path. Which repository the board *imports*
  issues from, when that differs from the one it opens issues in. Defaults to
  ``project``, which is the RFC's rule.
* ``aifactory_project_id`` — an AIFactory project UUID. What
  ``CFACTORY_INTAKE_PROJECT_ID`` has always actually held: the ``project_id``
  AIFactory's ``/api/tasks/from-issue`` and TFactory's ``/api/specs/ingest``
  both require and a card does not carry.

Defaulting the second to the first (as the RFC's single field would) would send
a repo path where a UUID is required, so they are kept apart and both are
editable in the panel.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .config import DEFAULT_TENANT, Settings
from .db import Base

logger = logging.getLogger(__name__)

# The provider types the board can be pointed at. A subset of the canonical
# ProviderType: bitbucket/gitea are declared there but unimplemented, and
# offering a setting that only ever raises would be a lie in the config surface.
# Kept here rather than in git_providers so the config layer does not have to
# import the provider layer to validate a field.
GITHUB, GITLAB, AZURE_DEVOPS = "github", "gitlab", "azure_devops"
SUPPORTED_PROVIDERS: tuple[str, ...] = (GITHUB, GITLAB, AZURE_DEVOPS)

# Where each provider talks when the tenant has not named a self-hosted instance.
# Present so a self-hosted GitLab / GitHub Enterprise / Azure DevOps Server works
# without a code change — that is the whole reason ``base_url`` is a field.
PROVIDER_DEFAULT_BASE_URL: dict[str, str] = {
    GITHUB: "https://api.github.com",
    GITLAB: "https://gitlab.com",
    AZURE_DEVOPS: "https://dev.azure.com",
}

# Azure DevOps addresses a repo as organization/project/repo, unlike the two-part
# path GitHub and GitLab use.
ADO_PATH_PARTS = 3

# A project path ("owner/repo", or "group/sub/project" on a GitLab with
# subgroups). Anchored and character-restricted because these strings end up
# interpolated into a request PATH by the provider: an unvalidated project
# carrying "../" or a host would let a config write choose which resource we
# call. Every segment must contain at least one alphanumeric, which is what keeps
# ".." out now that a path may be deeper than two segments. Lives here rather
# than in github_sync because a stored config is the first place a project path
# arrives from the outside — validation belongs at that boundary.
_SEGMENT = "[A-Za-z0-9_.-]*[A-Za-z0-9][A-Za-z0-9_.-]*"
PROJECT_PATTERN = rf"{_SEGMENT}(?:/{_SEGMENT})+"
PROJECT_RE = re.compile(rf"^({PROJECT_PATTERN})$")

# RFC-0011's intake trigger. A label with this prefix on an issue makes the fleet
# build it, so it must never arrive via ``default_labels`` — see
# :func:`validate_labels`.
_INTAKE_LABEL_PREFIX = "factory:"

# The derived status (RFC-0020 §3.3). Not a stored column: it is a function of
# the configuration plus the credential plus the last verification, and storing
# a value that three other things determine is how a status goes stale.
UNCONFIGURED = "unconfigured"
CONFIGURED = "configured"
CREDENTIAL_MISSING = "credential_missing"
VERIFIED = "verified"


def _now() -> datetime:
    return datetime.now(UTC)


class GitConfigError(ValueError):
    """A rejected git configuration. Rendered as a 400 over REST, an error
    payload over MCP — the same shape both surfaces already use."""


class GitConfigRow(Base):
    """One tenant's git configuration. Exactly one row per tenant."""

    __tablename__ = "tenant_git_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT
    )
    provider: Mapped[str] = mapped_column(String(32), default=GITHUB, server_default=GITHUB)
    # NULL means "the provider's public default" (PROVIDER_DEFAULT_BASE_URL), so
    # a tenant that never touches this keeps working if a default ever moves.
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    project: Mapped[str | None] = mapped_column(String(256), nullable=True)
    intake_project: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The AIFactory project id a dispatched card is BUILT in — an opaque UUID,
    # not a repository path. See the module docstring for why it is a separate
    # field from ``intake_project``.
    aifactory_project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Set by a successful verify, CLEARED by any edit: a verification proves the
    # config that was verified, so it cannot survive that config changing.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verify_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (Index("ix_tenant_git_config_tenant", "tenant_id", unique=True),)


class GitConfig(BaseModel):
    """The wire view of a tenant's git configuration.

    ``status`` is DERIVED on every read (see :func:`derive_status`), and
    ``source`` says whether the values came from the tenant's stored row or are
    still falling back to the deployment's environment.
    """

    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    provider: str
    # Always populated on a read — the tenant's own value, or the provider
    # default — so the panel can show where calls actually go.
    base_url: str
    project: str | None
    intake_project: str | None
    aifactory_project_id: str | None
    default_labels: list[str]
    status: str
    verified_at: datetime | None = None
    verify_error: str | None = None
    source: str  # "stored" | "env"


class GitConfigUpdate(BaseModel):
    """PUT body. A full replacement, not a patch: an omitted optional field is
    CLEARED, which is what makes "take the intake project back off" expressible."""

    provider: str = GITHUB
    base_url: str | None = Field(default=None, max_length=512)
    project: str | None = Field(default=None, max_length=256)
    intake_project: str | None = Field(default=None, max_length=256)
    aifactory_project_id: str | None = Field(default=None, max_length=128)
    default_labels: list[str] = Field(default_factory=list)


# ── validation ───────────────────────────────────────────────────────────────


def validate_provider(provider: str) -> str:
    kind = (provider or "").strip().lower()
    if kind not in SUPPORTED_PROVIDERS:
        raise GitConfigError(
            f"unknown provider: {provider!r} (expected one of {', '.join(SUPPORTED_PROVIDERS)})"
        )
    return kind


def validate_project(project: str | None, provider: str, *, field: str = "project") -> str | None:
    """A project path the configured provider can actually address.

    Rejected here rather than at call time because the alternative is a config
    that saves cleanly and then fails on every card write with an error nobody
    connects back to this form.
    """
    value = (project or "").strip()
    if not value:
        return None
    if PROJECT_RE.match(value) is None:
        raise GitConfigError(
            f"{field} must be a provider project path such as 'owner/repo' (got {project!r})"
        )
    if provider == AZURE_DEVOPS and len(value.split("/")) != ADO_PATH_PARTS:
        raise GitConfigError(f"{field} on azure_devops must be 'organization/project/repo'")
    return value


def validate_base_url(base_url: str | None) -> str | None:
    """A host root, or None for the provider's public default.

    A trust boundary: this string becomes an httpx ``base_url``, so anything that
    is not plainly an http(s) origin is refused rather than normalised.
    """
    value = (base_url or "").strip().rstrip("/")
    if not value:
        return None
    if not (value.startswith("http://") or value.startswith("https://")):
        raise GitConfigError("base_url must start with http:// or https://")
    return value


def validate_labels(labels: list[str] | None) -> list[str]:
    """Labels the board puts on issues it opens.

    ``factory:*`` is refused: RFC-0011's intake triggers on exactly that label,
    and the card being synced is already on its way into the factory — so a
    default label of ``factory:low`` would build every card twice. Refusing is
    better than silently dropping it, because a silently-dropped label looks
    saved and is not.
    """
    cleaned = [label.strip() for label in (labels or []) if label and label.strip()]
    intake = [label for label in cleaned if label.lower().startswith(_INTAKE_LABEL_PREFIX)]
    if intake:
        raise GitConfigError(
            f"default_labels must not contain the intake trigger {intake[0]!r}: a "
            "'factory:<tier>' label on an issue the board opens would build the same "
            "card a second time (RFC-0011)"
        )
    return cleaned


def validated_fields(update: GitConfigUpdate) -> dict[str, Any]:
    """The stored column values for a config write, or raise :class:`GitConfigError`."""
    provider = validate_provider(update.provider)
    return {
        "provider": provider,
        "base_url": validate_base_url(update.base_url),
        "project": validate_project(update.project, provider),
        "intake_project": validate_project(update.intake_project, provider, field="intake_project"),
        "aifactory_project_id": (update.aifactory_project_id or "").strip() or None,
        "default_labels": validate_labels(update.default_labels),
        # An edit invalidates any earlier verification: it proved a different
        # configuration.
        "verified_at": None,
        "verify_error": None,
    }


# ── credential (still environment-sourced until RFC-0020 phase 3) ────────────


def provider_token(settings: Settings) -> str | None:
    """The configured credential, or ``None`` when sync is not configured at all.

    ``git_provider_token`` is the provider-neutral name; ``github_token`` is the
    pre-RFC-0020 one and still works, so a deploy that set it keeps working
    untouched. Neither falls back to a bare ``GITHUB_TOKEN``/``GH_TOKEN`` — see
    the comment on those settings in :mod:`cfactory.config` for why that omission
    is load-bearing rather than an oversight.

    Deliberately NOT part of the stored configuration: credential custody is
    RFC-0020 §3.4 (phases 3 and 4). A tenant that has named a project but has no
    usable credential is exactly what ``credential_missing`` reports.
    """
    return settings.git_provider_token or settings.github_token


# ── resolution ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GitTarget:
    """Everything a consumer needs to talk to a tenant's git host.

    The ONE thing ``github_sync`` / ``issue_import`` / ``card_intake`` /
    ``git_providers`` read. They do not look at ``Settings`` for a provider, a
    repo or an intake project any more — if they did, the stored config would be
    a second opinion rather than the answer.
    """

    tenant: str
    provider: str
    base_url: str
    project: str | None
    # Where an import reads issues FROM. Defaults to ``project`` (RFC-0020 §3.3).
    import_project: str | None
    aifactory_project_id: str | None
    default_labels: tuple[str, ...]
    token: str | None
    stored: bool
    verified_at: datetime | None = None
    verify_error: str | None = None

    @property
    def status(self) -> str:
        return derive_status(project=self.project, token=self.token, verified_at=self.verified_at)


def derive_status(*, project: str | None, token: str | None, verified_at: Any) -> str:
    """The RFC-0020 §3.3 derived status, in the order the questions are asked.

    ``credential_missing`` outranks ``verified`` on purpose: a tenant whose token
    has been withdrawn since the last successful verification cannot reach its
    project now, and reporting the stale green would be the most misleading
    answer available.
    """
    if not project:
        return UNCONFIGURED
    if not token:
        return CREDENTIAL_MISSING
    return VERIFIED if verified_at else CONFIGURED


def target_from_settings(settings: Settings, tenant: str = DEFAULT_TENANT) -> GitTarget:
    """The target a deployment's ENVIRONMENT describes — the one-release bridge.

    Used for a tenant with no stored row (including every unit test that never
    writes one), and as the source the one-release seed materialises from.
    """
    provider = (settings.git_provider or GITHUB).strip().lower()
    base_url = (
        settings.git_provider_url
        or (settings.github_api_url if provider == GITHUB else None)
        or PROVIDER_DEFAULT_BASE_URL.get(provider, "")
    )
    project = (settings.github_repo or "").strip() or None
    return GitTarget(
        tenant=tenant,
        provider=provider,
        base_url=base_url,
        project=project,
        import_project=project,
        aifactory_project_id=(settings.intake_project_id or "").strip() or None,
        default_labels=(),
        token=provider_token(settings),
        stored=False,
    )


def target_from_row(row: GitConfigRow, settings: Settings) -> GitTarget:
    """The target a stored row describes. Only the credential comes from env."""
    provider = (row.provider or GITHUB).strip().lower()
    return GitTarget(
        tenant=row.tenant_id,
        provider=provider,
        base_url=row.base_url or PROVIDER_DEFAULT_BASE_URL.get(provider, ""),
        project=row.project,
        import_project=row.intake_project or row.project,
        aifactory_project_id=row.aifactory_project_id,
        default_labels=tuple(
            label for label in (row.default_labels or []) if isinstance(label, str)
        ),
        token=provider_token(settings),
        stored=True,
        verified_at=row.verified_at,
        verify_error=row.verify_error,
    )


def config_view(target: GitTarget) -> GitConfig:
    """The wire model for a resolved target."""
    return GitConfig(
        tenant_id=target.tenant,
        provider=target.provider,
        base_url=target.base_url,
        project=target.project,
        # Reported as the tenant set it, not as it resolves: showing ``project``
        # echoed back in the intake box would look like an edit nobody made.
        intake_project=target.import_project if target.import_project != target.project else None,
        aifactory_project_id=target.aifactory_project_id,
        default_labels=list(target.default_labels),
        status=target.status,
        verified_at=target.verified_at,
        verify_error=target.verify_error,
        source="stored" if target.stored else "env",
    )

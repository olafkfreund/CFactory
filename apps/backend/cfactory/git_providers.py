"""Which git host the board writes to (RFC-0020 phase 1).

Everything provider-SPECIFIC lives here; :mod:`cfactory.github_sync` above it
knows only :class:`IssueProvider` and ``IssueData`` and therefore knows nothing
about GitHub. That split is the whole point of the phase: the board's sync logic
stopped being a GitHub client and became a consumer of the fleet's abstraction.

The abstraction is not ours — it is the fleet's, vendored byte-for-byte from the
Factory hub at ``apps/backend/runners/github/`` (see that package's docstring)
and drift-gated in CI. GitLab and Azure DevOps issue support therefore arrives as
*canonical* code, not as a second hand-rolled client.

**Where the canonical does not (yet) fit, and why there is a GitHub class here.**
The hub's ``GitHubProvider`` drives the ``gh`` CLI through ``GHClient``. That is
right for the agent runners — they work inside a checkout on a machine where a
developer or a runner token has already logged ``gh`` in — but it is wrong twice
over for a hosted board:

1. the backend image has no ``gh`` binary (and adding one buys a subprocess per
   sync for an API call httpx already makes);
2. ``gh`` uses whatever credential is ambient, which is exactly the failure
   ``CFACTORY_GITHUB_TOKEN`` was introduced to prevent — issue-writing must be
   switched on by explicit config, never inherited from a login.

So GitHub is served here by :class:`HttpGitHubProvider`, an httpx implementation
of the same protocol carrying Phase 6's REST behaviour unchanged. The canonical
copy is NOT edited to fix this — the gap belongs in the hub (a token-authenticated
HTTP GitHub provider, or a ``GHClient`` that takes an explicit credential), and is
raised there rather than patched locally.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

import httpx
from runners.github.providers.factory import get_provider
from runners.github.providers.protocol import IssueData, ProviderType

from .config import Settings

_TIMEOUT_SECONDS = 10.0

_T = TypeVar("_T")

# The provider types the board can be pointed at. A subset of the canonical
# ProviderType: bitbucket/gitea are declared there but unimplemented, and
# offering a setting that only ever raises would be a lie in the config surface.
SUPPORTED_PROVIDERS = (
    ProviderType.GITHUB.value,
    ProviderType.GITLAB.value,
    ProviderType.AZURE_DEVOPS.value,
)

# Azure DevOps addresses a repo as organization/project/repo, unlike the two-part
# path GitHub and GitLab use.
_ADO_PATH_PARTS = 3


@runtime_checkable
class IssueProvider(Protocol):
    """The slice of the canonical ``GitProvider`` the board actually uses.

    Deliberately narrow. The full protocol covers pull requests, reviews, merges,
    labels and repo metadata; a planning board opens an issue and reads it back,
    and nothing else. Naming that subset means :class:`HttpGitHubProvider` does
    not have to carry fifteen ``NotImplementedError`` stubs to satisfy a type,
    while every canonical provider satisfies this structurally — which is checked,
    not assumed: ``build_provider`` returns canonical instances through it and the
    tests assert conformance.
    """

    @property
    def provider_type(self) -> ProviderType: ...

    @property
    def repo(self) -> str: ...

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> IssueData: ...

    async def fetch_issue(self, number: int) -> IssueData: ...


def run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run one provider call from the board's synchronous card path.

    The providers are async; the card write path is not. The REST endpoints are
    sync ``def`` (FastAPI hands those a worker thread, so ``asyncio.run`` would be
    legal), but the MCP endpoint is ``async def`` and dispatches its tools inline
    on the event loop, where ``asyncio.run`` raises. One short-lived thread is
    correct on both paths, so there is no branch to get wrong.

    ponytail: a thread per sync call. Board writes are rare and network-bound, so
    the thread is noise next to the HTTP round trip; revisit only if the card path
    ever becomes async end to end.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def provider_token(settings: Settings) -> str | None:
    """The configured credential, or ``None`` when sync is not configured at all.

    ``git_provider_token`` is the provider-neutral name; ``github_token`` is the
    pre-RFC-0020 one and still works, so a deploy that set it keeps working
    untouched. Neither falls back to a bare ``GITHUB_TOKEN``/``GH_TOKEN`` — see
    the comment on those settings in :mod:`cfactory.config` for why that omission
    is load-bearing rather than an oversight.
    """
    return settings.git_provider_token or settings.github_token


def _async_transport(
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None,
) -> httpx.AsyncBaseTransport | None:
    """Narrow the shared action-transport seam to the async client's type.

    Tests inject one ``httpx.MockTransport``, which implements both the sync and
    the async side; a sync-only transport is not usable here and is dropped rather
    than smuggled past the type.
    """
    return transport if isinstance(transport, httpx.AsyncBaseTransport) else None


@dataclass
class HttpGitHubProvider:
    """GitHub over its REST API, authenticated by an explicitly configured token.

    Phase 6's behaviour, moved behind the protocol and otherwise unchanged: same
    endpoints, same headers, same 10s timeout, same "no issue number in the
    response is an error" rule. See the module docstring for why this is not the
    canonical ``GitHubProvider``.
    """

    _repo: str
    _token: str | None = None
    _base_url: str = "https://api.github.com"
    _transport: httpx.AsyncBaseTransport | None = None

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.GITHUB

    @property
    def repo(self) -> str:
        return self._repo

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_TIMEOUT_SECONDS,
            transport=self._transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {self._token}",
            },
        )

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> IssueData:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        async with self._client() as client:
            resp = await client.post(f"/repos/{self._repo}/issues", json=payload)
            resp.raise_for_status()
            return self._parse_issue(resp.json())

    async def fetch_issue(self, number: int) -> IssueData:
        async with self._client() as client:
            resp = await client.get(f"/repos/{self._repo}/issues/{number}")
            resp.raise_for_status()
            return self._parse_issue(resp.json())

    def _parse_issue(self, issue: Any) -> IssueData:
        """GitHub's issue JSON as provider-neutral :class:`IssueData`.

        The number is the one field with no safe default: without it the caller
        cannot name the issue it just created, so a response missing it is an
        error rather than a card pointing at nothing.
        """
        issue = dict(issue) if isinstance(issue, dict) else {}
        number = issue.get("number")
        if not isinstance(number, int):
            raise ValueError(f"{self._repo}: response carried no issue number")
        return IssueData(
            number=number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            author=str((issue.get("user") or {}).get("login") or ""),
            state=str(issue.get("state") or ""),
            labels=[
                name
                for label in issue.get("labels") or []
                if isinstance(
                    name := (label.get("name") if isinstance(label, dict) else label), str
                )
            ],
            created_at=_parse_datetime(issue.get("created_at")),
            updated_at=_parse_datetime(issue.get("updated_at")),
            url=str(issue.get("html_url") or ""),
            assignees=[
                login
                for user in issue.get("assignees") or []
                if isinstance(login := (user.get("login") if isinstance(user, dict) else user), str)
            ],
            provider=ProviderType.GITHUB,
            raw_data=issue,
        )


def _parse_datetime(value: Any) -> datetime:
    """Mirrors the canonical providers' tolerant parse: a missing/odd timestamp is
    not worth failing a sync over, since the board mirrors none of them."""
    if not isinstance(value, str):
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def build_provider(
    settings: Settings,
    project: str,
    *,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
) -> IssueProvider:
    """The provider named by ``CFACTORY_GIT_PROVIDER``, pointed at *project*.

    ``project`` is the host's path for the repository: ``owner/repo`` on GitHub,
    ``group/project`` (subgroups allowed) on GitLab, ``organization/project/repo``
    on Azure DevOps. It comes from the card's own ``issue_ref`` when adopting, and
    from ``CFACTORY_GITHUB_REPO`` when opening.

    Raises ``ValueError`` on an unknown provider or a project path the provider
    cannot address — both are configuration errors, and the caller turns them into
    a visible ``github_sync_error`` on the card rather than a 500.
    """
    kind = (settings.git_provider or ProviderType.GITHUB.value).strip().lower()
    token = provider_token(settings)

    if kind == ProviderType.GITHUB.value:
        return HttpGitHubProvider(
            project,
            token,
            settings.git_provider_url or settings.github_api_url,
            _async_transport(transport),
        )

    if kind == ProviderType.GITLAB.value:
        kwargs: dict[str, Any] = {"_token": token}
        if settings.git_provider_url:
            kwargs["_base_url"] = settings.git_provider_url
        return get_provider(ProviderType.GITLAB, project, **kwargs)

    if kind == ProviderType.AZURE_DEVOPS.value:
        parts = project.split("/")
        if len(parts) != _ADO_PATH_PARTS:
            raise ValueError(f"azure_devops needs 'organization/project/repo', got {project!r}")
        organization, ado_project, repo = parts
        ado: dict[str, Any] = {
            "_pat": token,
            "_organization": organization,
            "_project": ado_project,
        }
        if settings.git_provider_url:
            ado["_base_url"] = settings.git_provider_url
        return get_provider(ProviderType.AZURE_DEVOPS, repo, **ado)

    raise ValueError(f"unknown git provider: {kind!r} (expected one of {SUPPORTED_PROVIDERS})")

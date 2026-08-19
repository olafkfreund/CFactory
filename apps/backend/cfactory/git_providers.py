"""Which git host the board writes to (RFC-0020 phases 1 and 2).

**Which host is a tenant's choice, not the process's.** :func:`build_provider`
takes a :class:`~cfactory.git_config.GitTarget` — the resolved tenant git
configuration (RFC-0020 §3.3) — rather than reading ``Settings``. Phase 1 read
``CFACTORY_GIT_PROVIDER`` here; phase 2 moved that decision into a stored,
portal-editable resource, and this module stopped having an opinion about where
the answer comes from.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

import httpx
from runners.github.providers.factory import get_provider
from runners.github.providers.protocol import (
    IssueComment,
    IssueData,
    IssueFilters,
    ProviderCommentError,
    ProviderType,
    oldest_first,
    to_iso_utc,
)

from .git_base_url import safe_git_base_url
from .git_config import (
    ADO_PATH_PARTS as _ADO_PATH_PARTS,
    PROVIDER_DEFAULT_BASE_URL,
    SUPPORTED_PROVIDERS,
    GitTarget,
    provider_token,
)

__all__ = [
    "SUPPORTED_PROVIDERS",
    "HttpGitHubProvider",
    "IssueProvider",
    "build_provider",
    "provider_token",
    "run_sync",
]

_TIMEOUT_SECONDS = 10.0
# GitHub's maximum page size for a list endpoint.
_GITHUB_PAGE_SIZE = 100
# Pages a comment read will walk before giving up (Factory#375). The cap bounds a
# repository-wide fetch (50 x 100 = 5000 comments) rather than paging forever
# against an unknown history size; hitting it is an ERROR, never a truncation —
# the same rule, and the same numbers, as the canonical GitHub provider.
_COMMENT_MAX_PAGES = 50

_T = TypeVar("_T")


@runtime_checkable
class IssueProvider(Protocol):
    """The slice of the canonical ``GitProvider`` the board actually uses.

    Deliberately narrow. The full protocol covers pull requests, reviews, merges
    and labels; a planning board opens an issue, reads it back, lists the
    project's issues to import them (RFC-0020 §3.6), and reads the repository
    once to verify a tenant's configuration (§3.3) — and nothing else.
    Naming that subset means :class:`HttpGitHubProvider` does
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

    async def fetch_issues(self, filters: IssueFilters | None = None) -> list[IssueData]: ...

    # The comment READ half of the protocol (Factory#375). Both are in the slice
    # because the board uses both and for different reasons: the bulk one is what
    # the incremental poll calls (one request covers the whole board where the
    # provider has a repository-wide endpoint), the single one is what a cold
    # backfill fans out over. Neither returns a partial thread — see
    # ``ProviderCommentError``.
    async def fetch_comments(
        self, issue_number: int, since: datetime | None = None
    ) -> list[IssueComment]: ...

    async def fetch_comments_bulk(
        self, issue_numbers: list[int], since: datetime | None = None
    ) -> dict[int, list[IssueComment]]: ...

    async def get_repository_info(self) -> dict[str, Any]: ...


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
    # repr=False so the credential cannot ride out in a traceback, a log line or
    # a debugger dump of this object. A dataclass renders every field by default,
    # which for a token-carrying object is a leak waiting for the first
    # ``logger.debug("provider=%s", provider)``.
    _token: str | None = field(default=None, repr=False)
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

    async def fetch_issues(self, filters: IssueFilters | None = None) -> list[IssueData]:
        """List the repo's issues, **paging to completion** up to ``limit``.

        The one provider here that pages. GitHub's list endpoint caps a page at
        100, so a repo with 300 open issues needs three calls, and taking only
        the first page would silently import a third of a backlog — the exact
        "where are my issues?" failure RFC-0020 §3.6 calls the worst outcome this
        feature has. The canonical providers do NOT page (see
        :func:`cfactory.issue_import.import_issues` for what that means there).

        ``/issues`` returns pull requests too — they carry a ``pull_request``
        key — so they are dropped unless explicitly asked for. A PR is not a
        plan.
        """
        filters = filters or IssueFilters()
        params: dict[str, Any] = {"state": filters.state, "per_page": _GITHUB_PAGE_SIZE}
        if filters.labels:
            params["labels"] = ",".join(filters.labels)
        if filters.assignee:
            params["assignee"] = filters.assignee
        if filters.author:
            params["creator"] = filters.author
        if filters.since is not None:
            # GitHub's `since` is "updated at or after", which is precisely the
            # watermark semantics the incremental pass wants.
            params["since"] = filters.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        issues: list[IssueData] = []
        async with self._client() as client:
            page = 1
            while len(issues) < filters.limit:
                resp = await client.get(
                    f"/repos/{self._repo}/issues", params={**params, "page": page}
                )
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for item in batch:
                    if (
                        not filters.include_prs
                        and isinstance(item, dict)
                        and "pull_request" in item
                    ):
                        continue
                    issues.append(self._parse_issue(item))
                if len(batch) < _GITHUB_PAGE_SIZE:
                    break
                page += 1
        return issues[: filters.limit]

    async def fetch_comments(
        self, issue_number: int, since: datetime | None = None
    ) -> list[IssueComment]:
        """Read ONE issue's comment thread (Factory#375).

        GitHub takes ``since`` server-side on the per-issue endpoint, so an
        incremental read transfers only what changed. This is the path a cold
        backfill fans out over — one call per card, bounded by the caller's list.
        """
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = to_iso_utc(since)
        path = f"/repos/{self._repo}/issues/{issue_number}/comments"
        raw = await self._comment_pages(path, params)
        return oldest_first([self._parse_comment(item, issue_number) for item in raw])

    async def fetch_comments_bulk(
        self, issue_numbers: list[int], since: datetime | None = None
    ) -> dict[int, list[IssueComment]]:
        """Read MANY issues' threads — the call the poll actually makes.

        ``GET /repos/{repo}/issues/comments`` streams every issue comment in the
        repository and honours ``since`` server-side, so one request answers the
        whole board: a 55-card refresh costs ONE call, not 55. That is what makes
        storing comments affordable at all, and it is the reason this method
        exists rather than a loop at the call site.

        Without ``since`` there is no window to narrow, so the repository-wide
        stream would page over the project's entire comment history to answer a
        question about a handful of issues. A cold backfill therefore fans out per
        issue instead — ``len(issue_numbers)`` calls, bounded by the caller's own
        list and independent of how big the repository is. Same reasoning, same
        shape, as the canonical ``GitHubProvider``; only the transport differs.
        """
        wanted = sorted({int(number) for number in issue_numbers})
        if not wanted:
            return {}
        if since is None:
            # The hub's ``fanout_comments`` is the same loop, but its signature
            # names the full canonical ``GitProvider`` and this class implements
            # the narrower :class:`IssueProvider` slice on purpose (see the module
            # docstring). Deduping and ordering are already done above, so the
            # helper would only add a cast.
            return {number: await self.fetch_comments(number, since=None) for number in wanted}

        raw = await self._comment_pages(
            f"/repos/{self._repo}/issues/comments",
            {"since": to_iso_utc(since), "sort": "updated", "direction": "asc"},
        )
        grouped: dict[int, list[IssueComment]] = {number: [] for number in wanted}
        for item in raw:
            number = _issue_number_from_url(item.get("issue_url", ""))
            # A repository-wide read answers for issues this board does not track
            # (and for pull-request comments, which share the endpoint); those are
            # dropped rather than stored against nothing.
            if number in grouped:
                grouped[number].append(self._parse_comment(item, number))
        return {number: oldest_first(comments) for number, comments in grouped.items()}

    async def _comment_pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Page through a comments endpoint, or FAIL — never truncate.

        A truncated thread stored as a whole one is indistinguishable from a short
        one, so every failure mode here raises :class:`ProviderCommentError` and
        the caller keeps whatever complete copy it already had.
        """
        collected: list[dict[str, Any]] = []
        async with self._client() as client:
            for page in range(1, _COMMENT_MAX_PAGES + 1):
                try:
                    resp = await client.get(
                        path, params={**params, "per_page": _GITHUB_PAGE_SIZE, "page": page}
                    )
                    resp.raise_for_status()
                    batch = resp.json()
                except Exception as exc:
                    raise ProviderCommentError(
                        f"GitHub comment read failed for {path} (page {page}): {exc}"
                    ) from exc
                if not isinstance(batch, list):
                    raise ProviderCommentError(
                        f"GitHub returned a non-list comment page for {path}: "
                        f"{type(batch).__name__}"
                    )
                collected.extend(item for item in batch if isinstance(item, dict))
                if len(batch) < _GITHUB_PAGE_SIZE:
                    return collected
        raise ProviderCommentError(
            f"GitHub comment read for {path} exceeded {_COMMENT_MAX_PAGES} pages; "
            "narrow the `since` window rather than accept a truncated thread"
        )

    def _parse_comment(self, data: dict[str, Any], issue_number: int) -> IssueComment:
        """GitHub's comment JSON as the provider-neutral :class:`IssueComment`."""
        user = data.get("user")
        author = user.get("login", "") if isinstance(user, dict) else str(user or "")
        created = data.get("created_at")
        return IssueComment(
            id=str(data.get("id", "")),
            issue_number=issue_number,
            author=author,
            body=data.get("body") or "",
            created_at=_parse_datetime(created),
            updated_at=_parse_datetime(data.get("updated_at") or created),
            url=data.get("html_url") or "",
            provider=ProviderType.GITHUB,
            raw_data=data,
        )

    async def get_repository_info(self) -> dict[str, Any]:
        """One cheap authenticated read of the repository (RFC-0020 §3.3 verify).

        The canonical providers all expose this, so the tenant git-config verify
        is the same single call on every host — and it is the *right* call: it
        proves in one round trip that the base URL resolves, the credential is
        accepted, and the project exists and is visible to it.
        """
        async with self._client() as client:
            resp = await client.get(f"/repos/{self._repo}")
            resp.raise_for_status()
            info = resp.json()
            return dict(info) if isinstance(info, dict) else {}

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
            milestone=_milestone_title(issue.get("milestone")),
            provider=ProviderType.GITHUB,
            raw_data=issue,
        )


def _issue_number_from_url(url: str) -> int | None:
    """Recover the issue number from a comment's ``issue_url``.

    The repository-wide comments endpoint does not carry the issue number as a
    field; the only link back is the URL it was made against.
    """
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _milestone_title(milestone: Any) -> str | None:
    """GitHub's milestone object as the title the card stores (RFC-0020 §3.6)."""
    if isinstance(milestone, dict):
        title = milestone.get("title")
        return title if isinstance(title, str) else None
    return milestone if isinstance(milestone, str) else None


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
    target: GitTarget,
    project: str,
    *,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
) -> IssueProvider:
    """The provider the TENANT's git config names, pointed at *project*.

    ``target`` is the resolved tenant configuration (RFC-0020 §3.3) — a stored
    row, or the deployment environment for a tenant that has none. It supplies
    the host kind, the base URL (so a self-hosted GitLab or GitHub Enterprise
    works) and the credential.

    ``project`` is passed separately rather than read off the target because the
    two differ on the path that matters most: adopting an issue uses the CARD's
    own project, not the tenant's configured one, so a card can point at a repo
    the board does not itself sync with.

    Raises ``ValueError`` on an unknown provider or a project path the provider
    cannot address — both are configuration errors, and the caller turns them into
    a visible ``github_sync_error`` on the card rather than a 500.

    **This is the credential injection point (RFC-0020 §3.4).** The target does
    not carry a token; it carries a handle, and the plaintext is fetched HERE,
    once, for the provider about to be built. Nothing above this line has it,
    nothing holds it after the provider is collected, and the fetch is what
    writes the audit entry. Every provider takes it as an explicit argument —
    there is no ambient-auth path in this backend to defend (see the module
    docstring on why ``gh`` is not used), so the leak surface is logs, exception
    text and API responses, and that is where the guards are.
    """
    kind = (target.provider or ProviderType.GITHUB.value).strip().lower()
    token = target.credential.token()
    # SSRF (#412). The stored base_url is caller-chosen and every branch below
    # hands it a real credential, so it is checked HERE, where it is read, and
    # the checked return value is what flows on.
    base_url = safe_git_base_url(target.base_url)

    if kind == ProviderType.GITHUB.value:
        return HttpGitHubProvider(
            project,
            token,
            base_url or PROVIDER_DEFAULT_BASE_URL[ProviderType.GITHUB.value],
            _async_transport(transport),
        )

    if kind == ProviderType.GITLAB.value:
        kwargs: dict[str, Any] = {"_token": token}
        if base_url:
            kwargs["_base_url"] = base_url
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
        if base_url:
            ado["_base_url"] = base_url
        return get_provider(ProviderType.AZURE_DEVOPS, repo, **ado)

    raise ValueError(f"unknown git provider: {kind!r} (expected one of {SUPPORTED_PROVIDERS})")

"""Federated cross-portal search over the aggregated WorkItem store (#149).

The cockpit already ingests every producer (PFactory/AIFactory/TFactory) into
one normalized ``WorkItem`` store, keyed by correlation key. That store is the
natural federated index: a single query here matches across all four portals'
work without fanning out to each service at request time.

This module is intentionally pure — it takes a materialized list of
``WorkItem`` and a query string and returns ranked result dicts. The route in
:mod:`cfactory.routes_workitems` is a thin wrapper so the scoring stays
unit-testable and the handler stays under the branch limit.
"""

from __future__ import annotations

from .models import ServiceState, WorkItem

SERVICES = ("pfactory", "aifactory", "tfactory")

# Field-level match scores (higher = more relevant). The best-scoring field on
# an item sets the item's score; ties break on recency.
_EXACT_KEY = 100
_PREFIX_KEY = 60
_SUBSTR_KEY = 40
_PREFIX_TITLE = 50
_SUBSTR_TITLE = 30
_SUBSTR_REPO = 20
_SUBSTR_STATE = 10  # status / phase / task_id


def _score_text(value: str | None, needle: str, prefix: int, substr: int) -> int:
    """Prefix/substring score for one text field (case-insensitive)."""
    if not value:
        return 0
    hay = value.lower()
    if hay.startswith(needle):
        return prefix
    if needle in hay:
        return substr
    return 0


def _score_service(slice_: ServiceState, needle: str) -> tuple[int, bool]:
    """Best score contributed by one service slice + whether repo matched."""
    repo_score = _score_text(slice_.repo, needle, _SUBSTR_REPO, _SUBSTR_REPO)
    state_score = max(
        _score_text(slice_.status, needle, _SUBSTR_STATE, _SUBSTR_STATE),
        _score_text(slice_.phase, needle, _SUBSTR_STATE, _SUBSTR_STATE),
        _score_text(slice_.task_id, needle, _SUBSTR_STATE, _SUBSTR_STATE),
    )
    return max(repo_score, state_score), repo_score > 0


def _score_item(wi: WorkItem, needle: str) -> tuple[int, list[str]]:
    """Return (best_score, matched_on) for one work item against a needle."""
    scores: dict[str, int] = {}

    key = wi.correlation_key.lower()
    if key == needle:
        scores["key"] = _EXACT_KEY
    elif key.startswith(needle):
        scores["key"] = _PREFIX_KEY
    elif needle in key:
        scores["key"] = _SUBSTR_KEY

    title_score = _score_text(wi.title, needle, _PREFIX_TITLE, _SUBSTR_TITLE)
    if title_score:
        scores["title"] = title_score

    for name in SERVICES:
        svc_score, repo_hit = _score_service(getattr(wi, name), needle)
        if svc_score:
            scores["repo" if repo_hit else "state"] = max(
                scores.get("repo" if repo_hit else "state", 0), svc_score
            )

    if not scores:
        return 0, []
    best = max(scores.values())
    matched_on = sorted(scores, key=lambda f: scores[f], reverse=True)
    return best, matched_on


def _services_present(wi: WorkItem) -> list[str]:
    """Portals that carry any state for this item (for result badges)."""
    return [name for name in SERVICES if getattr(wi, name).status]


def _overall_status(wi: WorkItem) -> str | None:
    """Furthest-stage status: test wins over build wins over plan."""
    for name in ("tfactory", "aifactory", "pfactory"):
        status = getattr(wi, name).status
        if status:
            return status
    return None


def _first_repo(wi: WorkItem) -> str | None:
    for name in SERVICES:
        repo = getattr(wi, name).repo
        if repo:
            return repo
    return None


def search_workitems(items: list[WorkItem], query: str, limit: int = 20) -> list[dict[str, object]]:
    """Rank ``items`` against ``query``; return the top ``limit`` result dicts.

    An empty/blank query returns nothing (the palette shows its own default
    view in that case). Results are sorted by score, then recency, then key so
    the ordering is deterministic.
    """
    needle = query.strip().lower()
    if not needle:
        return []

    scored: list[tuple[int, WorkItem, list[str]]] = []
    for wi in items:
        score, matched_on = _score_item(wi, needle)
        if score:
            scored.append((score, wi, matched_on))

    scored.sort(
        key=lambda row: (
            row[0],
            row[1].updated_at.timestamp() if row[1].updated_at else 0.0,
            row[1].correlation_key,
        ),
        reverse=True,
    )

    return [
        {
            "correlation_key": wi.correlation_key,
            "title": wi.title,
            "services": _services_present(wi),
            "repo": _first_repo(wi),
            "status": _overall_status(wi),
            "score": score,
            "matched_on": matched_on,
            "updated_at": wi.updated_at.isoformat() if wi.updated_at else None,
        }
        for score, wi, matched_on in scored[:limit]
    ]

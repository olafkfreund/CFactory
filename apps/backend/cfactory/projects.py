"""Project identities derived from a tenant's git repositories."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from . import git_config_ops
from .cards import CardStore


def slugify(name: str, fallback: str = "") -> str:
    normalized = unicodedata.normalize("NFKD", name).lower()
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", normalized)) or fallback


def derive_projects(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projects: dict[str, dict[str, Any]] = {}
    for repo in sorted(repos, key=lambda r: (r.get("created_at") or "", r["id"])):
        project_id = repo.get("aifactory_project_id") or f"repo:{repo['id']}"
        path = repo["project"]
        created_at = repo.get("created_at") or ""
        if project_id not in projects:
            projects[project_id] = {
                "id": project_id,
                "name": path.rsplit("/", 1)[-1] if repo.get("aifactory_project_id") else path,
                "created_at": created_at,
                "repository_ids": [],
                "repo_paths": [],
            }
        project = projects[project_id]
        project["repository_ids"].append(repo["id"])
        project["repo_paths"].append(path)
        if created_at and (not project["created_at"] or created_at < project["created_at"]):
            project["created_at"] = created_at

    result = list(projects.values())
    for project in result:
        pairs = sorted(
            zip(project["repository_ids"], project["repo_paths"], strict=True),
            key=lambda pair: pair[0],
        )
        primary_path = pairs[0][1]
        project["name"] = (
            primary_path if project["id"].startswith("repo:") else primary_path.rsplit("/", 1)[-1]
        )
        project["repository_ids"].sort()
        project["repo_paths"].sort(key=str.casefold)
    names = {project["id"]: project["name"] for project in result}
    for project in result:
        if not project["id"].startswith("repo:") and any(
            other is not project
            and names[other["id"]].lower() == project["name"].lower()
            and other["repo_paths"][0] != project["repo_paths"][0]
            for other in result
        ):
            project["name"] = project["repo_paths"][0]
    return sorted(result, key=lambda p: (p["created_at"] or "9999", p["id"]))


def _created(project: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(project["created_at"].replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0


def project_slugs(projects: list[dict[str, Any]]) -> dict[str, str]:
    out = {p["id"]: p["slug"] for p in projects if p.get("slug")}
    used = {slug.lower() for slug in out.values()}
    bases = [slugify(p["name"], slugify(p["id"], "project")) for p in projects]
    for project in sorted(
        (p for p in projects if not p.get("slug")), key=lambda p: (_created(p), p["id"])
    ):
        base = slugify(project["name"], slugify(project["id"], "project"))
        with_id = f"{base}-{slugify(project['id'], 'project')[:8]}"
        slug = with_id if base in used or bases.count(base) > 1 else base
        n = 2
        while slug.lower() in used:
            slug = f"{with_id}-{n}"
            n += 1
        used.add(slug.lower())
        out[project["id"]] = slug
    return out


def matching_projects(projects: list[dict[str, Any]], ref: str) -> list[dict[str, Any]]:
    lower = ref.lower()
    slugs = project_slugs(projects)
    canonical = [p for p in projects if slugs[p["id"]].lower() == lower]
    if canonical:
        return canonical
    ids = [p for p in projects if p["id"].lower() == lower]
    if ids:
        return ids
    return [
        p
        for p in projects
        if p.get("slug", "").lower() == lower
        or slugify(p["name"], p["id"]) == lower
        or f"{slugify(p['name'], p['id'])}-{slugify(p['id'], 'project')[:8]}" == lower
        or any(slugify(path, p["id"]) == lower for path in p["repo_paths"])
    ]


def find_project(projects: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    matches = matching_projects(projects, ref)
    return matches[0] if len(matches) == 1 else None


def list_projects(store: CardStore) -> list[dict[str, Any]]:
    projects = derive_projects(git_config_ops.list_git_repositories(store)["repositories"])
    slugs = project_slugs(projects)
    return [{**project, "slug": slugs[project["id"]]} for project in projects]


def resolve_project(store: CardStore, ref: str) -> dict[str, Any]:
    matches = matching_projects(list_projects(store), ref)
    return (
        {"project": matches[0]}
        if len(matches) == 1
        else {"error": "ambiguous" if matches else "unknown"}
    )

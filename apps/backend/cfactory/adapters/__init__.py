"""Service adapters (#7-#9) — hydrate the WorkItem store from upstream REST APIs."""

from __future__ import annotations

from cfactory.config import Settings, get_settings
from cfactory.store import WorkItemStore

from .aifactory import AIFactoryAdapter
from .base import AdapterError, AdapterItem, BaseHTTPAdapter, ServiceProbe
from .pfactory import PFactoryAdapter
from .tfactory import TFactoryAdapter

__all__ = [
    "AIFactoryAdapter",
    "AdapterError",
    "AdapterItem",
    "BaseHTTPAdapter",
    "PFactoryAdapter",
    "ServiceProbe",
    "TFactoryAdapter",
    "build_adapters",
    "hydrate",
]


def build_adapters(settings: Settings | None = None) -> list[BaseHTTPAdapter]:
    """One adapter per service, pointed at the configured endpoints and carrying
    its own bearer token, else the shared upstream one (when set) for authenticated calls."""
    settings = settings or get_settings()
    return [
        PFactoryAdapter(settings.pfactory_api_url, token=settings.token_for("pfactory")),
        AIFactoryAdapter(settings.aifactory_api_url, token=settings.token_for("aifactory")),
        TFactoryAdapter(settings.tfactory_api_url, token=settings.token_for("tfactory")),
    ]


def hydrate(
    store: WorkItemStore, items: list[AdapterItem], newest_ids: frozenset[str] = frozenset()
) -> int:
    """Upsert a batch of normalized items into the store; return the count."""
    for item in items:
        store.upsert_snapshot(
            item.correlation_key,
            item.service,
            item.to_state(),
            title=item.title,
            newest=item.task_id in newest_ids,
        )
    return len(items)

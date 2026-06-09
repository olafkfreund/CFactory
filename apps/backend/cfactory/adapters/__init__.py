"""Service adapters (#7-#9) — hydrate the WorkItem store from upstream REST APIs."""

from __future__ import annotations

from ..config import Settings, get_settings
from ..store import WorkItemStore
from .aifactory import AIFactoryAdapter
from .base import AdapterError, AdapterItem, BaseHTTPAdapter
from .pfactory import PFactoryAdapter
from .tfactory import TFactoryAdapter

__all__ = [
    "AIFactoryAdapter",
    "AdapterError",
    "AdapterItem",
    "BaseHTTPAdapter",
    "PFactoryAdapter",
    "TFactoryAdapter",
    "build_adapters",
    "hydrate",
]


def build_adapters(settings: Settings | None = None) -> list[BaseHTTPAdapter]:
    """One adapter per service, pointed at the configured endpoints and carrying
    the shared upstream bearer token (when set) for authenticated calls."""
    settings = settings or get_settings()
    token = settings.upstream_token
    return [
        PFactoryAdapter(settings.pfactory_api_url, token=token),
        AIFactoryAdapter(settings.aifactory_api_url, token=token),
        TFactoryAdapter(settings.tfactory_api_url, token=token),
    ]


def hydrate(store: WorkItemStore, items: list[AdapterItem]) -> int:
    """Upsert a batch of normalized items into the store; return the count."""
    for item in items:
        store.upsert_snapshot(
            item.correlation_key, item.service, item.to_state(), title=item.title
        )
    return len(items)

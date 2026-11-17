"""Service adapters (#7-#9) — hydrate the WorkItem store from upstream REST APIs."""

from __future__ import annotations

import logging

from cfactory.config import Settings, get_settings
from cfactory.store import WorkItemStore

from .aifactory import AIFactoryAdapter
from .base import AdapterError, AdapterItem, BaseHTTPAdapter, ServiceProbe
from .pfactory import PFactoryAdapter
from .tfactory import TFactoryAdapter

logger = logging.getLogger(__name__)

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
    "live_ids_all_orgs",
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


def live_ids_all_orgs(
    adapter: BaseHTTPAdapter, items: list[AdapterItem], extra_tokens: list[str]
) -> set[str] | None:
    """Task ids live across the primary org and every extra org-bound key (#517).

    Reconcile needs the whole picture or it blanks other orgs' running stages.
    Returns ``None`` when any extra org cannot be listed: skip reconcile then,
    a partial view must never clear anything."""
    ids = {i.task_id for i in items}
    for token in extra_tokens:
        sibling = adapter.with_token(token)
        try:
            ids |= {i.task_id for i in sibling.list_items()}
        except AdapterError as exc:
            # Never log the token or error text; the class names the cause (401 = revoked key).
            logger.warning(
                "reconcile skipped for %s: an extra org key could not be listed (%s)",
                adapter.service.value,
                type(exc).__name__,
            )
            return None
        finally:
            sibling.close()
    return ids

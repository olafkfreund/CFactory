"""Tenant scoping of the work-item store and cockpit APIs (#172).

Covers the four contract points: migration/backfill of a pre-#172 database,
stamp-on-write, filter-on-read with CFACTORY_MULTI_TENANT on, and byte-for-byte
current behaviour with the flag off. The real ``store_dep`` (not an override)
is exercised for the API tests, with ``get_settings``/``get_store`` patched in
the ``cfactory.api_deps`` namespace where the dependency reads them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory import api_deps
from cfactory.app import create_app
from cfactory.config import Settings, resolve_tenant
from cfactory.models import CompletionEvent, Service
from cfactory.store import WorkItemStore
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

# ── helpers ──────────────────────────────────────────────────────────────────


def _event(key: str, event_id: str = "ev-1") -> CompletionEvent:
    return CompletionEvent(
        id=event_id,
        correlation_key=key,
        service=Service.PFACTORY,
        task_id="pf-task",
        status="planned",
        phase="pfactory",
        updated_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
    )


def _patch(monkeypatch, store: WorkItemStore, *, multi_tenant: bool) -> TestClient:
    """Wire the REAL store_dep to a temp store + a forced multi_tenant flag."""
    settings = Settings(multi_tenant=multi_tenant)
    monkeypatch.setattr(api_deps, "get_settings", lambda: settings)
    monkeypatch.setattr(api_deps, "get_store", lambda: store)
    return TestClient(create_app())


# ── migration / backfill ─────────────────────────────────────────────────────


def test_pre_172_db_gets_tenant_column_backfilled_to_default(tmp_path):
    """A database created before #172 (no tenant_id column) is upgraded on
    store init and its existing rows backfilled to the 'default' tenant."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE work_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "correlation_key VARCHAR(128) UNIQUE, "
                "title VARCHAR(512), pfactory JSON, aifactory JSON, "
                "tfactory JSON, timeline JSON, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO work_items (correlation_key, timeline, pfactory, "
                "aifactory, tfactory, created_at, updated_at) VALUES "
                "('legacy-1', '[]', '{}', '{}', '{}', "
                "'2026-07-01 00:00:00', '2026-07-01 00:00:00')"
            )
        )
    engine.dispose()

    store = WorkItemStore(url)  # init runs the tenant-column guard
    assert store.get("legacy-1") is not None  # SELECT works post-ALTER
    # Backfilled row belongs to the default tenant, invisible to others.
    assert store.scoped("default").get("legacy-1") is not None
    assert store.scoped("acme").get("legacy-1") is None
    # Guard is idempotent: re-init on the same file is a no-op.
    assert WorkItemStore(url).get("legacy-1") is not None


# ── stamp on write ───────────────────────────────────────────────────────────


def test_scoped_write_stamps_tenant(store):
    store.scoped("acme").upsert_from_event(_event("42"))
    assert store.scoped("acme").get("42") is not None
    assert store.scoped("globex").get("42") is None
    # The unscoped store (single-tenant mode) still sees everything.
    assert store.get("42") is not None


def test_unscoped_write_stamps_default_tenant(store):
    store.upsert_from_event(_event("42"))
    assert store.scoped("default").get("42") is not None
    assert store.scoped("acme").get("42") is None


def test_scoped_delete_cannot_cross_tenants(store):
    store.scoped("acme").upsert_from_event(_event("42"))
    crossed = store.scoped("globex").delete("42")
    assert crossed is False
    owned = store.scoped("acme").delete("42")
    assert owned is True


# ── filter on read, flag ON ──────────────────────────────────────────────────


def test_api_filters_by_tenant_when_multi_tenant_on(monkeypatch, store):
    store.scoped("acme").upsert_from_event(_event("acme-1", "ev-a"))
    store.scoped("globex").upsert_from_event(_event("globex-1", "ev-g"))
    client = _patch(monkeypatch, store, multi_tenant=True)

    body = client.get("/api/workitems", headers={"X-Tenant-Id": "acme"}).json()
    assert body["count"] == 1
    assert body["items"][0]["correlation_key"] == "acme-1"

    # Detail: own item resolves, the other tenant's is a 404.
    assert client.get("/api/workitems/acme-1", headers={"X-Tenant-Id": "acme"}).status_code == 200
    assert client.get("/api/workitems/globex-1", headers={"X-Tenant-Id": "acme"}).status_code == 404

    # No header resolves to the default tenant, which owns neither item.
    assert client.get("/api/workitems").json()["count"] == 0


def test_api_ingest_stamps_resolved_tenant_when_on(monkeypatch, store):
    client = _patch(monkeypatch, store, multi_tenant=True)
    resp = client.post(
        "/api/events",
        json=_event("55").model_dump(mode="json"),
        headers={"X-Tenant-Id": "acme"},
    )
    assert resp.status_code == 200
    assert store.scoped("acme").get("55") is not None
    assert store.scoped("default").get("55") is None


# ── flag OFF: current behaviour exactly ──────────────────────────────────────


def test_api_ignores_tenant_header_when_multi_tenant_off(monkeypatch, store):
    # Even rows stamped with a non-default tenant stay visible with the flag
    # off — the read path applies no filter at all (pre-#172 behaviour).
    store.scoped("acme").upsert_from_event(_event("acme-1", "ev-a"))
    store.upsert_from_event(_event("plain-1", "ev-p"))
    client = _patch(monkeypatch, store, multi_tenant=False)

    assert client.get("/api/workitems").json()["count"] == 2
    body = client.get("/api/workitems", headers={"X-Tenant-Id": "globex"}).json()
    assert body["count"] == 2
    assert client.get("/api/workitems/acme-1").status_code == 200


# ── resolver ─────────────────────────────────────────────────────────────────


def test_resolve_tenant():
    off = Settings(multi_tenant=False)
    on = Settings(multi_tenant=True)
    assert resolve_tenant("acme", off) == "default"
    assert resolve_tenant("acme", on) == "acme"
    assert resolve_tenant("  acme  ", on) == "acme"
    assert resolve_tenant(None, on) == "default"
    assert resolve_tenant("   ", on) == "default"

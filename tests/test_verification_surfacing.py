"""RFC-0006 (#76): CFactory captures the verification block from the event."""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.models import CompletionEvent, Service

_VERIFICATION = {
    "target_level": "VAL-2",
    "achieved_level": "VAL-2",
    "claim": "Verified to VAL-2. NOT verified: VAL-3 not_run (no disposable target).",
    "levels": [
        {"level": "VAL-1", "status": "passed"},
        {"level": "VAL-2", "status": "passed"},
        {"level": "VAL-3", "status": "not_run", "reason": "no disposable target"},
    ],
    "_gate": {"violations": [], "downgraded": False},
}


def _tfactory_event(*, verification=None, key="42"):
    return CompletionEvent(
        correlation_key=key,
        service=Service.TFACTORY,
        task_id="t-1",
        status="triaged",
        phase="test",
        updated_at=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        verification=verification,
    )


def test_verification_lands_on_the_tfactory_slice(store):
    item, _ = store.upsert_from_event(_tfactory_event(verification=_VERIFICATION))
    block = item.tfactory.extra["verification"]
    assert block["achieved_level"] == "VAL-2"
    assert "NOT verified: VAL-3" in block["claim"]


def test_verification_preserved_in_timeline(store):
    item, _ = store.upsert_from_event(_tfactory_event(verification=_VERIFICATION))
    ev = item.timeline[-1]
    assert ev.verification["achieved_level"] == "VAL-2"


def test_no_verification_leaves_slice_clean(store):
    item, _ = store.upsert_from_event(_tfactory_event(verification=None))
    assert "verification" not in (item.tfactory.extra or {})

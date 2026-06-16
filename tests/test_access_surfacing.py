"""RFC-0007 (#88 PR-1): CFactory captures the access annotation from the event."""

from __future__ import annotations

from datetime import datetime, timezone

from cfactory.models import CompletionEvent, Service

_ACCESS = {
    "val3": "not_run",
    "reason": "access not available for: mfa",
    "risk": "credentialed/system (VAL-3) behavior unproven",
    "blocked": [{"resource": "mfa", "reason": "push approval"}],
}


def _tfactory_event(*, access=None, key="42"):
    return CompletionEvent(
        correlation_key=key,
        service=Service.TFACTORY,
        task_id="t-1",
        status="triaged",
        phase="test",
        updated_at=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        access=access,
    )


def test_access_lands_on_the_tfactory_slice(store):
    item, _ = store.upsert_from_event(_tfactory_event(access=_ACCESS))
    assert item.tfactory.extra["access"]["val3"] == "not_run"
    assert item.tfactory.extra["access"]["blocked"][0]["resource"] == "mfa"


def test_access_preserved_in_timeline(store):
    item, _ = store.upsert_from_event(_tfactory_event(access=_ACCESS))
    ev = item.timeline[-1]
    # timeline stores the event dict; the access annotation round-trips
    assert ev.access["reason"] == "access not available for: mfa"


def test_no_access_leaves_slice_clean(store):
    item, _ = store.upsert_from_event(_tfactory_event(access=None))
    assert "access" not in (item.tfactory.extra or {})

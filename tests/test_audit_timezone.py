"""Audit and activity timestamps must be unambiguous UTC instants (#258).

The stored value was always a UTC wall clock, but `DateTime` (no `timezone=True`)
reads back NAIVE, so the API served `2026-07-30T11:40:19.386322` with no offset.
`new Date(iso)` on an offsetless date-time string is defined (ES2015+) to be
interpreted in the RUNTIME'S LOCAL ZONE, so under `Europe/London` in summer the
browser read a 11:40 UTC entry as 11:40 BST = 10:40 UTC and reported an hour of
elapsed time that never passed. An action taken seconds ago read "60m ago".

The test below reproduces that hour, rather than merely asserting a `tzinfo` is
present: it parses the served string the way a browser does, in a fixed
`Europe/London` zone, and compares the resulting instant to the truth.

The fix is at the serialisation boundary only. It deliberately does NOT touch
the HMAC canonical form: `_canonical_ts` already converts to UTC and strips
tzinfo, so an aware and a naive UTC value render to the same canonical string
and `verify()` is unaffected for entries written before or after this change.
That property is pinned by `test_chain_survives_timezone_awareness` below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from cfactory.audit import AuditStore, compute_entry_hash

_LONDON = ZoneInfo("Europe/London")

# A summer instant, when Europe/London is UTC+1. The bug is invisible in winter.
_BST_INSTANT = datetime(2026, 7, 30, 11, 40, 19, 386322, tzinfo=UTC)


def _as_a_browser_would(iso: str) -> datetime:
    """Parse a served timestamp the way `new Date(iso)` does in Europe/London.

    An offsetless date-time string is interpreted in the local zone; one that
    carries an offset is an absolute instant regardless of the local zone.
    """
    parsed = datetime.fromisoformat(iso)
    return parsed.replace(tzinfo=_LONDON) if parsed.tzinfo is None else parsed


def _store(tmp_path) -> AuditStore:
    return AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="test-secret")


def _record(store: AuditStore) -> None:
    store.record(
        actor="olaf",
        kind="approve_review",
        correlation_key="cf-258",
        target_service="aifactory",
        endpoint="/api/review/approve",
        status_code=200,
        ok=True,
    )


def test_a_fresh_entry_is_not_displayed_an_hour_in_the_past(tmp_path) -> None:
    """The reported symptom: an action taken seconds ago read "60m ago" (#258).

    This is the whole bug, measured end to end in the units the user saw. It
    fails by exactly 3600s on the pre-fix tree.
    """
    store = _store(tmp_path)
    before = datetime.now(UTC)
    _record(store)
    served = store.list()[0].model_dump(mode="json")

    skew = before - _as_a_browser_would(str(served["ts"]))
    assert skew < timedelta(seconds=30), (
        f"a just-recorded entry renders {skew} in the past. The served ts "
        f"{served['ts']!r} carries no UTC offset, so a Europe/London browser "
        "reads it as BST and back-dates it by the DST offset."
    )


def test_served_ts_is_an_absolute_instant(tmp_path) -> None:
    """The served string must mean the same instant in every viewer's zone."""
    store = _store(tmp_path)
    _record(store)
    served_ts = str(store.list()[0].model_dump(mode="json")["ts"])

    assert datetime.fromisoformat(served_ts).utcoffset() is not None, (
        f"served ts {served_ts!r} has no offset, so its meaning depends on who "
        "is reading it. An audit record whose instant is reader-dependent is "
        "not evidence (Factory#310, EU AI Act Art. 14)."
    )


def test_chain_survives_timezone_awareness(tmp_path) -> None:
    """Entries written before the fix must still verify after it (#258, #21).

    `ts` is inside `_CANONICAL_FIELDS`, so this is the property that decides
    whether the fix is shippable at all: the canonical rendering of a naive UTC
    value and of the same instant made aware must be byte-identical, or every
    pre-existing row's `entry_hash` stops matching and tamper-evidence is lost.
    """
    store = _store(tmp_path)
    _record(store)
    _record(store)
    assert store.verify() == [], "chain broken by the timezone fix"

    naive = {
        "ts": _BST_INSTANT.replace(tzinfo=None),
        "actor": "olaf",
        "kind": "approve_review",
        "correlation_key": "cf-258",
        "target_service": "aifactory",
        "endpoint": "/api/review/approve",
        "status_code": 200,
        "ok": True,
    }
    aware = {**naive, "ts": _BST_INSTANT}
    assert compute_entry_hash("s", naive, None) == compute_entry_hash("s", aware, None), (
        "the canonical form is not invariant to tz-awareness; making stored "
        "timestamps aware would invalidate every entry_hash written before it."
    )


def test_an_offsetless_producer_timestamp_is_read_as_utc(client, store) -> None:
    """The Activity panel showed the same skew, from the producer's clock (#258).

    The upstreams stamp UTC and (still) send it without an offset. CFactory
    cannot invent a zone, but "no offset" from a fleet service unambiguously
    means UTC, so normalising it at the ingest boundary is the honest read —
    and it is the only place all three producers route through.
    """
    from cfactory.models import CompletionEvent, Service

    store.upsert_from_event(
        CompletionEvent(
            correlation_key="cf-258",
            service=Service.AIFACTORY,
            task_id="t-1",
            status="completed",
            updated_at=_BST_INSTANT.replace(tzinfo=None),  # as an upstream sends it
        )
    )
    rows = client.get("/api/activity").json()["activity"]
    assert rows, "expected an activity row"
    for row in rows:
        served = datetime.fromisoformat(str(row["updated_at"]))
        assert served.utcoffset() is not None, (
            f"activity updated_at {row['updated_at']!r} has no offset; the "
            "Activity table back-dates by the DST offset exactly as /api/audit did."
        )
        assert served == _BST_INSTANT, (
            f"{row['updated_at']!r} is not the instant the producer meant "
            f"({_BST_INSTANT.isoformat()}) — the offset must LABEL the existing "
            "UTC wall clock, never shift it."
        )

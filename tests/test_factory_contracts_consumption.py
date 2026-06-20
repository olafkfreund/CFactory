"""Proof that CFactory consumes the generated factory-contracts (Factory#160).

These tests lock the consumption end of the single source of truth:

1. ``cfactory.status_taxonomy`` re-exports the SHARED classifiers (the backend no
   longer hand-rolls its own) and they keep token-boundary semantics.
2. ``cfactory.models.TokenUsage`` DERIVES from the shared ``Usage`` base while
   preserving the exact prior JSON shape (zero defaults, extra ignored).
3. The shared RFC-0001 ``CompletionEvent`` envelope round-trips a CFactory wire
   payload — the envelope contract the two sides agree on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cfactory import status_taxonomy
from cfactory._contracts import factory_contracts as fc
from cfactory.models import CompletionEvent, Service, TokenUsage

# --------------------------------------------------------------------------
# 1. The taxonomy is the SHARED module, not a local copy.
# --------------------------------------------------------------------------


def test_taxonomy_classifiers_are_the_shared_ones() -> None:
    """Every classifier the backend imports IS the shared package's function."""
    for name in (
        "is_done",
        "is_failed",
        "is_review",
        "is_queued",
        "is_stuck",
        "is_terminal",
        "is_failure_or_stuck",
        "is_running",
        "is_active",
    ):
        assert getattr(status_taxonomy, name) is getattr(fc, name), name


def test_taxonomy_token_sets_are_the_shared_ones() -> None:
    assert status_taxonomy._DONE_TOKENS is fc._DONE_TOKENS
    assert status_taxonomy._FAILED_TOKENS is fc._FAILED_TOKENS


def test_substring_bug_stays_fixed_via_shared_module() -> None:
    # 'ready' is done; 'already' merely contains those letters.
    assert status_taxonomy.is_done("ready") is True
    assert status_taxonomy.is_done("already") is False
    assert status_taxonomy.is_failed("failsafe_passed") is False


# --------------------------------------------------------------------------
# 2. TokenUsage derives from the shared Usage, JSON shape preserved.
# --------------------------------------------------------------------------


def test_token_usage_derives_from_shared_usage() -> None:
    assert issubclass(TokenUsage, fc.Usage)


def test_token_usage_zero_defaults_preserved() -> None:
    """The historic zero defaults survive the derivation (shared base is None)."""
    t = TokenUsage()
    assert t.input_tokens == 0
    assert t.output_tokens == 0
    assert t.total_tokens == 0
    assert t.cost_usd == 0.0
    assert t.model is None
    # v1.3 breakdowns default absent.
    assert t.workers is None
    assert t.by_provider is None


def test_token_usage_ignores_unknown_fields_like_before() -> None:
    """CFactory still ignores unknown wire fields (not the base's extra=allow)."""
    t = TokenUsage(input_tokens=5, totally_unknown="x")  # type: ignore[call-arg]
    dumped = t.model_dump()
    assert "totally_unknown" not in dumped
    assert dumped["input_tokens"] == 5


def test_token_usage_json_shape_unchanged() -> None:
    """The serialized field set is exactly the documented CFactory shape."""
    keys = set(TokenUsage().model_dump().keys())
    assert keys == {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "model",
        "workers",
        "by_provider",
        "by_model",
        "budget",
    }


# --------------------------------------------------------------------------
# 3. The shared CompletionEvent envelope round-trips a CFactory payload.
# --------------------------------------------------------------------------


def _cf_event() -> CompletionEvent:
    return CompletionEvent(
        correlation_key="142",
        service=Service.AIFACTORY,
        task_id="proj:001",
        status="done",
        phase="act",
        updated_at=datetime(2026, 6, 5, 12, 0, tzinfo=UTC),
        usage=TokenUsage(input_tokens=2400, output_tokens=100, total_tokens=2500, cost_usd=1.25),
    )


def test_shared_envelope_round_trips_cfactory_payload() -> None:
    """A CFactory event serialized to JSON parses into the shared envelope."""
    payload = _cf_event().model_dump(mode="json")
    shared = fc.CompletionEvent.model_validate(payload)
    assert shared.correlation_key == "142"
    assert shared.service == "aifactory"  # shared envelope uses the str value
    assert shared.status == "done"
    assert shared.usage is not None
    assert shared.usage.total_tokens == 2500
    assert shared.usage.cost_usd == 1.25
    # CFactory-specific fields the shared envelope doesn't name are preserved
    # (extra="allow" on the shared model), never dropped.
    round_tripped = shared.model_dump()
    assert round_tripped["correlation_key"] == "142"


def test_shared_usage_rollup_helper_sums_cfactory_usages() -> None:
    """The shared rollup helper folds CFactory TokenUsage instances correctly."""
    total = fc.rollup_usage(
        [
            TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.5),
            TokenUsage(input_tokens=2, total_tokens=2, cost_usd=0.1),
        ]
    )
    assert total.input_tokens == 12
    assert total.total_tokens == 17
    assert total.cost_usd == 0.6

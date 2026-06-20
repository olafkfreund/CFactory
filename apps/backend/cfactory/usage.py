"""Shared token/cost rollup primitives (#116).

One home for the per-provider/per-model rollup shape so the bucket key tuple is
not repeated across ``store.rollup_by`` and ``copilot.tools._merge_rollup``. Pure
+ side-effect-free (no imports of store/copilot) so both layers can depend on it.
"""

from __future__ import annotations

# The numeric fields summed in every token/cost rollup bucket. ``cost_usd`` is a
# float; the token counts are ints — both accumulate the same way.
ROLLUP_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cost_usd")


def empty_bucket() -> dict:
    """A fresh rollup bucket: all ``ROLLUP_KEYS`` zeroed plus a ``workers`` count."""
    bucket: dict = dict.fromkeys(ROLLUP_KEYS, 0)
    bucket["workers"] = 0
    return bucket


def add_usage(dst: dict, src: dict | None, *, count_worker: bool = False) -> None:
    """Sum the token/cost fields of ``src`` into the ``dst`` rollup bucket in place.

    Adds each ``ROLLUP_KEYS`` field; when ``count_worker`` is set the source is a
    single worker record and the ``workers`` count is incremented by one,
    otherwise the source is itself a rollup carrying its own ``workers`` count
    which is added through. Missing/None source fields are treated as zero.
    """
    for k in ROLLUP_KEYS:
        dst[k] += src.get(k, 0) or 0 if src else 0
    if count_worker:
        dst["workers"] += 1
    elif src:
        dst["workers"] += src.get("workers", 0) or 0

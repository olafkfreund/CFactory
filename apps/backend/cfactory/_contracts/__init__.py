"""Vendored, pinned copy of the Factory hub's generated ``factory-contracts``.

This package holds a BYTE-IDENTICAL copy of the hub's single source of truth for
the cross-factory completion-event envelope (RFC-0001), its v1.1 token-usage
block, the usage rollup helpers, and the lifecycle status taxonomy:

    Factory/shared/factory-contracts/python/factory_contracts/__init__.py
        @ 48f59a2c0a875f1637f4a442c1da4a9cd5fd1db9   (Factory#160 / PR #168)

The hub file is GENERATED (``scripts/gen_contracts.py``) and committed; CFactory
PINS it here rather than hand-rolling its own status classifiers and usage shapes
(epic Factory#154, issue Factory#160). CFactory is the canonical taxonomy the
contract was generated FROM, so the token sets are identical by construction; the
vendored copy lets the backend IMPORT the shared module instead of duplicating
it, and a drift gate (``tests/test_factory_contracts_drift.py`` +
``contracts-drift`` CI job) fails if this copy diverges from the pinned hub SHA.

DO NOT EDIT ``factory_contracts/__init__.py`` by hand: it is the hub's generated
output verbatim. To update, re-vendor from the hub and bump the SHA above.
"""

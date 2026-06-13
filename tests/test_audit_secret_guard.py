"""Tests for the startup audit-secret guard (#81): a hosted/shared deploy must
not silently boot on the forgeable in-repo dev HMAC default.

The guard hard-warns (rather than hard-failing) so that constructing the app in
unit tests stays non-breaking; the warning fires only on a real boot posture
(API keys or multi-tenant configured) that still carries the dev default.
"""

from __future__ import annotations

import warnings

from cfactory.config import (
    DEV_AUDIT_HMAC_SECRET,
    Settings,
    check_audit_secret,
    is_local_only,
)


def test_local_only_dev_default_is_quiet():
    # Single-user local mode: no keys, no multi-tenant — the dev default is fine.
    settings = Settings(audit_hmac_secret=DEV_AUDIT_HMAC_SECRET)
    assert is_local_only(settings) is True
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert check_audit_secret(settings) is False


def test_api_keys_with_dev_default_warns():
    settings = Settings(
        audit_hmac_secret=DEV_AUDIT_HMAC_SECRET, api_keys="k_rw:read,write"
    )
    assert is_local_only(settings) is False
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert check_audit_secret(settings) is True
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_multi_tenant_with_dev_default_warns():
    settings = Settings(audit_hmac_secret=DEV_AUDIT_HMAC_SECRET, multi_tenant=True)
    assert is_local_only(settings) is False
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert check_audit_secret(settings) is True
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_real_secret_in_hosted_mode_is_quiet():
    # A real secret in a hosted posture is the correct, safe state — no warning.
    settings = Settings(audit_hmac_secret="a-real-rotated-secret", multi_tenant=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert check_audit_secret(settings) is False

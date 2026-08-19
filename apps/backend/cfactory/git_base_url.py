"""One SSRF check for a git connection's stored ``base_url`` (#412).

Its own leaf module, rather than a private helper in ``git_providers`` or in a
routes file, because three unrelated readers need it -- the provider factory,
the install callback and the install-token mint -- and the strict lint bar bans
the relative and function-local imports that sharing it between those packages
would otherwise need (TID252 / PLC0415).

This adds no SSRF logic of its own. It calls
``factory_common.url_safety.assert_safe_outbound_url``, the fleet canonical
guard this backend already vendors (cf. TFactory#1111, which is about not
growing a second dialect of this check). The public name of that function is
registered BY NAME as a barrier in the repo's CodeQL pack, so calling it -- and
using the value it returns -- is what clears the taint, here and to the analyser.

Ports TFactory#1116 / PFactory#611, which closed the identical defect in the two
sibling backends.
"""

from __future__ import annotations

from factory_common.url_safety import assert_safe_outbound_url

from .git_config import PROVIDER_DEFAULT_BASE_URL, GitConfigError

# The provider defaults are this repo's own constants, not caller input, and
# ``resolved_base_url`` substitutes one whenever a connection names no host -- so
# the common case is a value that was never untrusted. Short-circuiting them
# keeps a getaddrinfo off every provider build and keeps the guard from turning a
# DNS blip into a config error on a path that has no attacker in it.
_TRUSTED_DEFAULTS = frozenset(PROVIDER_DEFAULT_BASE_URL.values())


def safe_git_base_url(base_url: str | None) -> str | None:
    """SSRF-check a connection's ``base_url`` before a credential rides on it.

    ``base_url`` is stored per git connection and settable over
    ``PUT /api/tenants/{tenant}/git-config`` and the ``/git-connections`` routes.
    Those require ``require_scope("write")`` and that the path tenant match the
    resolved identity -- and nothing more. There is no operator or admin role in
    front of them, and in OPEN mode (no keystore configured) ``require_scope``
    returns without checking anything at all. So an ordinary board user, not an
    operator, chooses this host.

    The stored value then addresses requests that carry real secrets: the tenant
    credential on every provider call (``git_providers.build_provider``, the
    RFC-0020 §3.4 injection point), the deployment's GitLab client secret and a
    GitHub App JWT on the install callback, and a refresh token at mint time.

    :func:`~cfactory.git_config.validate_base_url` already runs on the write, but
    it only asserts the string starts with ``http://`` or ``https://``. A scheme
    says nothing about where the host resolves, and
    ``http://169.254.169.254/latest/meta-data/`` passes it. That is also why a
    scheme test is deliberately NOT registered as a barrier in this fleet's
    CodeQL packs.

    The check sits at each read, and NOT in
    ``runners/github/providers/factory.py`` or in ``factory_common/url_safety``:
    both are byte-gated vendored canonicals shared across four repos
    (``factory-github-drift`` / ``factory-common-drift``). The untrusted value
    arrives at CFactory's own trust boundary, so the guard belongs here.

    ``allow_private=True``: a self-hosted GitLab CE/EE, GitHub Enterprise or
    Azure DevOps Server on a LAN is the entire reason ``base_url`` is a field, so
    refusing RFC-1918 would break real deployments. Both postures still refuse
    the cloud-metadata range, which is never a git host and is the one with a
    credential-harvesting payoff.

    Returns the checked URL, so a caller is forced to use the value the check
    actually saw rather than re-reading the setting.

    Residual, stated rather than implied: DNS rebinding. The guard resolves the
    host and the transport resolves it again at connect time. Closing that needs
    IP-pinning at the socket layer -- the same residual every caller of
    ``url_safety`` carries.
    """
    if not base_url:
        return base_url
    if base_url.rstrip("/") in _TRUSTED_DEFAULTS:
        return base_url
    try:
        return assert_safe_outbound_url(base_url, allow_private=True)
    except ValueError as exc:
        raise GitConfigError(f"refusing this git base_url: {exc}") from exc

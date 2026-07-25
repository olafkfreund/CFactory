"""The fleet's canonical git-provider layer, vendored (RFC-0020 phase 1).

``gh_client.py``, ``rate_limiter.py`` and ``providers/`` are a **byte-identical**
copy of the Factory hub's single source of truth at ``shared/factory-github/``,
taken at hub commit ``15475fd87316a4812ea24a347873c1d7b1013f26``. This is the
same vendor-canonical + drift-gate model AIFactory, PFactory and TFactory use;
``.github/workflows/factory-github-drift.yml`` fetches the canonical at that
pinned SHA and fails the build if a byte here differs.

**Do not edit anything in this tree.** A fix belongs in the hub canonical (which
is CODEOWNERS-reviewed, because a change here is a fleet change); then re-vendor
and bump the pinned SHA. Editing the copy to silence the gate is precisely the
silent divergence the gate exists to stop.

CFactory consumes ``providers.protocol.GitProvider`` from here — see
:mod:`cfactory.git_providers` for the wiring, including the one place where the
canonical does not (yet) serve a hosted service: its ``GitHubProvider`` drives
the ``gh`` CLI, which a backend container has neither the binary nor the ambient
credentials for.
"""

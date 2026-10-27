---
status: approved
issue: olafkfreund/Factory#312
spec: spec/2026-10-26-312-mcp-legacy-secret-mode.md
---

# Plan: count and gate the legacy CFACTORY_MCP_SECRET on /mcp

Approved decisions (from the spec):
- New setting `CFACTORY_MCP_LEGACY_SECRET_MODE`, default `allow`. Only `allow` (case-insensitive, stripped) lets the legacy secret authenticate; any other value denies it (fail closed).
- Every legacy-secret match is counted in a module-level `Counter[str]` in `mcp.py` keyed `allowed`/`denied`, and logs a WARNING with the running total and the sanitised User-Agent. Never the secret or token.
- In deny mode, a matching token falls through to the scoped keystore (it keeps only a scoped grant if it is also a registered key), else 401 `Invalid MCP token`. Never dev-open.
- Follows the CFactory#501 `events_auth_mode` pattern. No client code changes in any repo. No gitops, no rotation.

## Steps

1. `apps/backend/cfactory/config.py` (after `mcp_secret`, ~line 257): add `mcp_legacy_secret_mode: str = "allow"` with a short comment. `apps/backend/cfactory/mcp.py`: import `Counter` from `collections`; add `_legacy_mcp_uses: Counter[str] = Counter()`; in `_authenticate` (~line 993) replace step 1 per the decisions; update the module docstring auth list. `tests/test_mcp.py`: add tests (legacy allowed + counted + secret not logged; deny → 401 + counted; unknown mode → 401; scoped key works in deny). `docs/dev/environment-reference.md` (row after `CFACTORY_MCP_SECRET`, line 53) and `.env.example` (near line 89): document the variable.
   → verify by `apps/backend/.venv/bin/pytest tests/test_mcp.py -q` and `apps/backend/.venv/bin/ruff check` + `ruff format --check` on touched files, `python scripts/ratchet_lint.py` if it runs on these files.
   Traps: `config.reset_settings()` and `auth.reset_keystore()` after env changes in tests; `sanitize_log` is already imported in `mcp.py`; pre-commit runs the test suite (let it run, never `--no-verify`).

## Tests

`apps/backend/.venv/bin/pytest tests/test_mcp.py tests/test_token_gated_api.py -q`: all pass.

## Rollback

Revert the commit. The default is `allow`, so no deploy depends on it.

## Deviations

- Step 1: the warning is logged from two branches with constant `allowed`/`denied` strings, instead of one call that formats an outcome computed from `mcp_legacy_secret_mode`. CodeQL `py/clear-text-logging-sensitive-data` treats any value derived from a setting named `*secret*` as sensitive. The counter is named `_legacy_mcp_uses` (not `_legacy_secret_uses`) for the same reason. The behaviour and log content are unchanged.

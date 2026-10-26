---
status: draft
issue: olafkfreund/Factory#312
intent: intent/2026-10-26-312-api-fail-closed.md
---

# Spec: CFactory /api and /connect fail closed when no keys are configured

## Design

This mirrors what `/mcp` already does (RFC-0019 Phase 2a, `mcp_dev_open`).

- `apps/backend/cfactory/config.py`: new setting `api_dev_open: bool = False` (`CFACTORY_API_DEV_OPEN`), next to `mcp_dev_open`.
- `apps/backend/cfactory/auth.py`:
  - `KeyStore.__init__(keys, dev_open=False)`. `replace()` keeps `dev_open`.
  - New property `open`, true when there are no keys and `dev_open` is set. `configured` is unchanged, because `mcp.py` and `enterprise.py` read it for their own precedence.
  - `authorize()` with no keys: returns if `dev_open`, otherwise raises 401 `"no API keys configured: set CFACTORY_API_KEYS (or CFACTORY_API_DEV_OPEN=true for local dev only)"` with `WWW-Authenticate: Bearer`.
  - `authorize_headers()` and `require_scope()` change their early return from `not keystore.configured` to `keystore.open`, so a keyless, non-dev store reaches `authorize()` and is denied.
  - `get_keystore()` builds `KeyStore(keys, dev_open=settings.api_dev_open)`.
- All `/api/*` and `/connect/*` HTTP traffic, plus the WebSocket handlers in `api_deps.py`, already go through `authorize_headers`/`require_scope`, so one change covers all of them. The existing exemptions are unchanged: `/health`, `/api/events*`, `/mcp` and the docs.
- `flake.nix` dev shell: `export CFACTORY_API_DEV_OPEN="${CFACTORY_API_DEV_OPEN:-true}"` next to the MCP one, so `nix develop` keeps today's behaviour.
- `tests/conftest.py`: set `CFACTORY_API_DEV_OPEN=true` in the environment before the app is imported, so the existing suite keeps testing OPEN-mode routes as dev mode. Deny is tested explicitly.
- Docs: `.env.example`, `docs/dev/environment-reference.md`, `techdocs/dependencies.md` and the `charts/cfactory/values.yaml` `apiKeys` comment now say "unset = DENY unless CFACTORY_API_DEV_OPEN".

## Alternatives rejected

- **Refuse to start when no keys are set and the bind is non-loopback** (the sibling `DISABLE_AUTH` pattern). The bind host is a uvicorn CLI flag (`Dockerfile`), not a setting CFactory can see. A boot failure also takes down `/health`, which turns a config miss into a crash loop instead of a loud 401.
- **A `CFACTORY_REQUIRE_API_KEYS` flag that defaults to off.** It leaves the unsafe default in place, which is the defect the epic names. `/mcp` already chose default-deny with a dev opt-in, and one convention is easier to operate than two.
- **Deny only writes and leave reads open.** Reads expose the board, the audit trail and git configuration. The epic says "no keys = deny".
- **A Helm knob for the flag.** Hosted deploys must never set it, and `config.extraEnv` already exists for anyone who must.

## Risks

- A developer running uvicorn outside the dev shell with no keys gets 401s. The 401 message names both fixes.
- Helm installs with `apiKeys.enabled: false` stop serving the API openly. This is intended. The chart comment says so.
- Live cluster: none. `CFACTORY_API_KEYS` is set from a non-empty secret (checked read-only), so `configured` is true and the new branch never runs.

## Verification

- New tests: an empty keystore denies `/api` (401, message names `CFACTORY_API_KEYS`) on HTTP and WebSocket; with `dev_open` it allows; a configured keystore ignores `dev_open`; `/health` and `/api/events` stay exempt; `get_keystore()` reads `CFACTORY_API_DEV_OPEN`.
- `test_execute_open_when_keystore_explicitly_empty` becomes a deny test, and it fails on the old code.
- Full backend pytest, ruff, and the mypy ratchet stay green.

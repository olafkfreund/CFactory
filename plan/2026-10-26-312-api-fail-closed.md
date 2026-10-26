---
status: approved
issue: olafkfreund/Factory#312
spec: spec/2026-10-26-312-api-fail-closed.md
---

# Plan: CFactory /api and /connect fail closed when no keys are configured

Decisions carried over from the approved spec:

- An empty keystore DENIES `/api/*` and `/connect/*` (HTTP and WebSocket) with a 401 that names `CFACTORY_API_KEYS`, unless `CFACTORY_API_DEV_OPEN=true` is set.
- The flag is ignored once keys are configured.
- `KeyStore.configured` keeps its meaning, so the `/mcp` and enterprise precedence is untouched.
- The nix dev shell exports the flag as true.
- The test suite runs in dev-open by default (set in conftest) and tests deny explicitly.
- No Helm knob, no gitops change, no `/mcp` change. Live has keys, so it is unaffected.

## Steps

1. `apps/backend/cfactory/config.py` (next to `mcp_dev_open`, ~line 255): add `api_dev_open: bool = False`, with a comment explaining it.
   `apps/backend/cfactory/auth.py`:
   - `KeyStore.__init__(self, keys=None, dev_open: bool = False)` stores `self.dev_open`.
   - Add an `open` property: `not self._keys and self.dev_open`.
   - `authorize()`: when not configured, return if `dev_open`, else raise `HTTPException(401, "no API keys configured: set CFACTORY_API_KEYS (or CFACTORY_API_DEV_OPEN=true for local dev only)", headers={"WWW-Authenticate": "Bearer"})`.
   - `authorize_headers()` and `require_scope()`: change `if not keystore.configured` to `if keystore.open`.
   - `get_keystore()`: pass `dev_open=get_settings().api_dev_open`.
   - Update the module, class and function docstrings that say "OPEN mode always allows".
   → verify by `cd apps/backend && .venv/bin/ruff check cfactory/auth.py cfactory/config.py`.
   Traps: `replace()` must keep `dev_open`. Do not change `configured`, because `mcp.py:998/1005` and `enterprise.py:199` rely on it.
2. `tests/conftest.py`: `os.environ.setdefault("CFACTORY_API_DEV_OPEN", "true")` before `from cfactory.app import ...`, with a one-line why.
   `tests/test_auth.py`:
   - `test_execute_open_when_keystore_explicitly_empty` becomes `..._denied_...` and expects 401 with `CFACTORY_API_KEYS` in the detail.
   - Add a `KeyStore({}, dev_open=True)` variant that expects 200.
   `tests/test_token_gated_api.py`:
   - With `monkeypatch.setattr(get_keystore(), "dev_open", False)`: `GET /api/...` returns 401, the `/api/ws` WebSocket is rejected, and `/health` and `/api/events` stay reachable.
   - A configured keystore with `dev_open=True` still 401s an unknown key.
   - `get_keystore()` reads `CFACTORY_API_DEV_OPEN` (monkeypatch env, then `reset_settings()` and `reset_keystore()`).
   → verify by `cd apps/backend && .venv/bin/pytest ../../tests/test_auth.py ../../tests/test_token_gated_api.py -q`, which must fail on the old auth.py for the deny tests.
   Traps: the existing `_restore_keystore` fixture calls `reset_keystore()`; keep it.
3. `flake.nix`: export `CFACTORY_API_DEV_OPEN="''${CFACTORY_API_DEV_OPEN:-true}"` next to the MCP one.
   `.env.example`, `docs/dev/environment-reference.md`, `techdocs/dependencies.md`: change "unset = OPEN" to "unset = DENY unless CFACTORY_API_DEV_OPEN" and add a `CFACTORY_API_DEV_OPEN` row or line.
   `charts/cfactory/values.yaml`: update the `apiKeys` comment.
   → verify by `grep -rn 'Unset = auth OPEN\|auth is OPEN' .env.example docs techdocs charts` returning nothing.
   Traps: none.

## Tests

- `cd apps/backend && .venv/bin/pytest ../../tests -q`: all green.
- `ruff check` and `ruff format --check` on the touched Python files.
- `python3 scripts/ratchet_lint.py`, if present.

## Rollback

Revert the PR. As an emergency stopgap on a keyless deploy, set `CFACTORY_API_DEV_OPEN=true`. The proper fix is to set `CFACTORY_API_KEYS`.

---
status: draft
issue: olafkfreund/Factory#312
intent: intent/2026-10-26-312-mcp-legacy-secret-mode.md
---

# Spec: count and gate the legacy CFACTORY_MCP_SECRET on /mcp

## Design

Follow the existing `CFACTORY_EVENTS_AUTH_MODE` pattern (CFactory#501: a string mode in `Settings`, a module-level `Counter`, a warning log).

- `apps/backend/cfactory/config.py`: add `mcp_legacy_secret_mode: str = "allow"` (env `CFACTORY_MCP_LEGACY_SECRET_MODE`), next to `mcp_secret`.
- `apps/backend/cfactory/mcp.py`, `_authenticate`, precedence step 1: when the presented token matches `mcp_secret`:
  - mode (stripped, lower-cased) `allow`: increment `_legacy_secret_uses["allowed"]`, log a WARNING naming the deprecation, the running total and the sanitised `User-Agent` (so operators can see which client to migrate), and return `FULL_SCOPE` as today.
  - any other value (`deny`, a typo, empty): increment `_legacy_secret_uses["denied"]`, log a WARNING, and do not grant anything. Fall through to the scoped keystore (step 2) so a token that is also a registered scoped key keeps exactly its scoped grant; otherwise step 3 answers 401 `Invalid MCP token`. Because `mcp_secret` is still set, step 3 never reaches dev-open: fail closed.
- The log never contains the secret or the presented token.
- No client-side code changes: no repo-tracked client sends the secret (see intent). Clients switch by changing the bearer value.
- Docs: add the variable to `docs/dev/environment-reference.md`, `.env.example` and the auth list in the `mcp.py` docstring.

## Alternatives rejected

- **Reject outright in deny mode (no fall-through to the keystore).** A token that is legitimately a scoped key would be refused because it also equals the legacy secret. Falling through gives it its scoped grant only, never full scope.
- **Validate the mode in `Settings` and refuse to boot on an unknown value.** Turns a typo into an outage of the whole cockpit; the events mode already fails closed per request, so stay consistent.
- **Prometheus counter.** CFactory exposes no metrics endpoint; the #501 precedent is a counter plus log line. Adding a metrics stack is out of scope.
- **Changing the factories' or factory-vscode's client code.** None of them reads `CFACTORY_MCP_SECRET`; they send an opaque bearer, so there is nothing to change.
- **Removing the secret or flipping gitops now.** Out of this slice; listed in the intent.

## Risks

- A noisy log on prod: every legacy call logs one WARNING. That is the point (it measures migration), and the parr-run watcher polls at human pace.
- Someone sets `deny` before clients move: the Claude Code `cfactory` server and parr-run get 401. Mitigated by the default and the ordered steps in the intent.

## Verification

`apps/backend/.venv/bin/pytest tests/test_mcp.py -q` with new tests:
- legacy secret in default mode: 200, read and write tools, counter `allowed` +1, warning logged, secret absent from the log;
- `deny` mode: legacy secret gets 401, counter `denied` +1;
- an unknown mode value (e.g. `denyy`): legacy secret gets 401;
- a scoped key still works in `deny` mode.
Plus `ruff check`/`ruff format --check` on touched files and the mypy ratchet if present.

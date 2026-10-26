---
status: approved
issue: olafkfreund/Factory#312
author: olafkfreund
---

# Intent: count and gate the legacy CFACTORY_MCP_SECRET on /mcp

## Problem

The epic olafkfreund/Factory#312 has a second wildcard credential, found in its comment of 2026-08-10: `CFACTORY_MCP_SECRET`. On CFactory `POST /mcp` it is checked first and grants read AND write (`apps/backend/cfactory/mcp.py`, `_authenticate`). The scoped replacement, `CFACTORY_API_KEYS`, already gates `/mcp` per tool scope, but nothing shows who still uses the legacy secret and there is no switch to turn it off. In gitops the secret is the shared `APP_API_TOKEN`, so it cannot be dropped blind.

## Proposed outcome

- Every `/mcp` call authenticated by the legacy secret is counted and logs a deprecation warning (never the secret). The logs show when use reaches zero.
- A new `CFACTORY_MCP_LEGACY_SECRET_MODE` setting: `allow` (default, today's behaviour) or `deny` (the legacy secret no longer authenticates). Any other value fails closed, as `deny`.
- Scoped `CFACTORY_API_KEYS` keys keep working in every mode.

Nothing changes on the live cluster: the default is `allow`.

## Affected users and systems

- CFactory backend: `mcp.py`, `config.py`, tests, env docs.
- Clients of CFactory `/mcp`, enumerated on 2026-10-26 across CFactory, AIFactory, PFactory, TFactory, Factory, factory-gitops and factory-vscode:
  - No repo-tracked code or `.mcp.json` sends the legacy secret. The three `.mcp.json` files (AIFactory, PFactory, TFactory) have no CFactory entry.
  - factory-vscode registers `/mcp` with whatever bearer the user configured (OIDC, `factory.cfactoryToken`, or stored token). It can already send a scoped key; no change needed.
  - Operator-local, outside any repo: the Claude Code `cfactory` MCP server entry in the user's `~/.claude.json`, and the `parr-run` skill's `cf_mcp` helper, both send the shared `APP_API_TOKEN`. They only need their bearer value swapped for a scoped key once one is minted.
  So this slice needs a PR in CFactory only.

## Constraints

- Additive; default behaviour unchanged. No gitops, cluster or credential change; no rotation; no deploy.
- Never log the secret or the presented token.
- An unknown mode value must fail closed.

## Open questions

None.

## Remaining steps after this slice (in order, not in this PR)

1. Keystore: mint a scoped `CFACTORY_API_KEYS` entry per MCP client (`read` for the parr-run watcher, `read,write` for the Claude Code board client) in the `factory-secrets` / sealed secret that feeds the `cfactory` Deployment.
2. Operator-local: point `~/.claude.json` `mcpServers.cfactory` and the `parr-run` `cf_mcp` helper at the new keys.
3. Watch the deprecation warning count reach zero in the `cfactory` logs.
4. gitops: set `CFACTORY_MCP_LEGACY_SECRET_MODE=deny` on the `cfactory` Deployment (`factory-gitops/apps/cfactory/manifests/manifests.yaml`).
5. gitops: remove the `CFACTORY_MCP_SECRET` env entry (it reuses `APP_API_TOKEN`), and update the cloudflared comment that names it.
6. Code: delete the legacy branch and the mode setting from CFactory.
7. Rotate `APP_API_TOKEN` once its other edges are retired (rest of #312).

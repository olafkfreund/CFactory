---
status: draft
issue: olafkfreund/Factory#312
author: olafkfreund
---

# Intent: CFactory /api and /connect fail closed when no keys are configured

## Problem

The epic olafkfreund/Factory#312 (IAM and access control) lists "remove/gate `DISABLE_AUTH`; make CFactory fail-closed (no keys = deny)".

`DISABLE_AUTH` is already gated in AIFactory, PFactory and TFactory: each refuses to boot with auth disabled on a non-loopback host without an explicit opt-in. That part of the epic needs no new slice; see "Remaining slices" for the one leftover.

CFactory is not gated. When `CFACTORY_API_KEYS` is empty the keystore is in OPEN mode (`apps/backend/cfactory/auth.py`): every request to `/api/*` and `/connect/*` is allowed, including the write endpoints that dispatch work into the factory. The server binds `0.0.0.0` in the image (`Dockerfile`), and the Helm chart's default is `apiKeys.enabled: false`. So any deploy that forgets the keys exposes the full cockpit API, writes included, with no authentication. `/mcp` was already changed to deny in this case (RFC-0019 Phase 2a, `CFACTORY_MCP_DEV_OPEN`). `/api` and `/connect` were left behind.

## Proposed outcome

With no keys configured, `/api/*` and `/connect/*` (HTTP and WebSocket) return a loud 401 naming `CFACTORY_API_KEYS`, unless `CFACTORY_API_DEV_OPEN=true` is set. The nix dev shell sets the flag to true, so `nix develop` keeps working as it does today. Once keys are configured, the flag is ignored and nothing changes.

## Affected users and systems

- CFactory backend: `auth.py`, `config.py`, and the test fixtures.
- Local developers using the nix dev shell: unchanged, because the shell exports the flag.
- Local developers running the backend outside the dev shell with no keys: they must set `CFACTORY_API_DEV_OPEN=true`.
- Helm chart installs with `apiKeys.enabled: false`: the API now denies instead of serving open.
- Live cluster: unaffected. Checked read-only on 2026-10-26: the `cfactory` Deployment sets `CFACTORY_API_KEYS` from a non-empty secret, and `CFACTORY_MCP_SECRET` is also set. No factory Deployment sets `APP_DISABLE_AUTH`.

## Constraints

- Must not lock out any human or service login on the live cluster. The cluster has keys, so the new branch never runs there.
- No gitops, cluster or credential change; no deploy.
- Must not touch `CFACTORY_MCP_SECRET` precedence or `/mcp` behaviour.
- Must not touch AIFactory `mcp_stdio` files (AIFactory #1885 is in flight).
- `/health`, `/api/events*` and the docs stay exempt, as today.

## Open questions

None. The default changes for keyless non-dev-shell runs. That is the point of the epic item, and no live deploy relies on it.

## Remaining slices (in order)

1. **This slice:** CFactory `/api` and `/connect` fail closed with no keys.
2. AIFactory: give `DISABLE_AUTH` off loopback its own opt-in (`APP_ALLOW_INSECURE_AUTH`, as in PFactory and TFactory) instead of reusing `APP_DEBUG`, which is set for other reasons.
3. Retire `CFACTORY_MCP_SECRET`: move MCP clients to scoped `CFACTORY_API_KEYS` entries, then drop the legacy bearer (code first, then gitops).
4. Retire the shared `APP_API_TOKEN` per edge (PFactory→AIFactory, AIFactory→TFactory, intake→PFactory, cockpit→all) in favour of `acw_` service keys; finish Factory#555.
5. Enforce MFA/TOTP (+ WebAuthn) as a Keycloak required action, declaratively in gitops.
6. Fleet RBAC role model and the quarterly access-review evidence.

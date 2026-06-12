# CFactory: Connecting Editors & External Clients (all scenarios)

The `factory-vscode` extension authenticates to CFactory with
`Authorization: Bearer <token>` on every REST call and the `/api/ws` WebSocket.
How it gets that token depends on how the deployment is secured. CFactory is
built so the editor can connect **in every scenario**, not just under SSO:

| Deployment | Front-door auth | How the editor connects |
|---|---|---|
| **Local / dev** | none (open) | No token needed — the extension sends no `Authorization`; the open API accepts it. Nothing to configure. |
| **Self-hosted, no SSO** | CFactory API key | **This runbook (Path B).** Enable the keystore; the editor uses `/connect/vscode` (one-click) or `/settings/token` (manual). |
| **Keycloak SSO** | oauth2-proxy | Either Path B, **or** the OIDC convenience layer (Path A) below for a smoother login. |
| **Other proxy** (Authelia, basic-auth, mTLS) | proxy-specific | Path B API key on a direct host, or open + a network ACL. |

The **CFactory API key (Path B)** is the universal mechanism — it depends on
nothing but CFactory, so it works with or without Keycloak. Keycloak OIDC
(Path A) is an optional convenience that only applies where Keycloak exists.
Both are inert until enabled: in **OPEN mode** (no `CFACTORY_API_KEYS`) the API
accepts every request, so local/dev needs no setup at all. The extension tries
them in order (OIDC → token setting → stored bearer), so the two coexist.

---

## Path B — CFactory API key (universal)

The browser cockpit may sit behind a front-door (e.g. oauth2-proxy). That gate
works for a human in a browser, but **not** for the editor, which can only
present a bearer token. Path B turns on a **token-gated API surface** so the
editor authenticates with a CFactory API key, while the browser cockpit keeps
working unchanged.

## How it fits together

```
Browser  ──▶ cloudflared ──▶ oauth2-proxy ──▶ cockpit nginx ──▶ backend
            cfactory.…                          (injects the key)   (keystore)

Editor   ──▶ cloudflared ─────────────────────────────────────▶ backend
            cfactory-api.…   (no oauth2-proxy; editor's own bearer)  (keystore)
```

- **Backend keystore enforcement** (`apps/backend/cfactory/auth.py` + the
  `enforce_api_key` middleware in `app.py`): when `CFACTORY_API_KEYS` is set,
  every `/api/*` and `/connect/*` request needs a `read`-scoped key; write
  endpoints additionally need `write`. WebSocket handlers reject unauthenticated
  sockets. Exempt: `/health` (probes), `/mcp` (own secret), `/api/events*` (the
  idempotent inbound webhook). In **OPEN mode** (no keys) nothing is enforced —
  the local/dev default.
- **Cockpit nginx injection** (`nginx.conf.template`): the frontend injects
  `Authorization: Bearer <key>` on its `/api` + `/connect` proxies via the
  `CFACTORY_API_KEY` env. Browser users are already authenticated by oauth2-proxy
  upstream, so the cockpit keeps working once the keystore is enforced.
- **Editor host**: a cloudflared hostname (e.g. `cfactory-api.freundcloud.org.uk`)
  routed straight to `cfactory:3111`, bypassing oauth2-proxy. The editor's own
  bearer is the gate, enforced by the keystore — the same pattern as the existing
  `cfactory-mcp` host.

## Enabling it (staged — no cockpit lockout)

The danger is enforcing the keystore before nginx injects the key: the cockpit
would 401. Stage the rollout so the injecting frontend is live **first**.

1. **Create the Secret** (one key, two forms — the scoped string for the backend,
   the bare key for nginx injection):
   ```
   kubectl -n factory create secret generic cfactory-api-keys \
     --from-literal=api-keys='acw_xxxxxxxx:read,write' \
     --from-literal=api-key='acw_xxxxxxxx'
   ```
2. **Stage 1 — inject, keystore still open.** Set `frontend.apiKey.enabled=true`
   (nginx now injects the real key) while leaving `apiKeys.enabled=false`.
   Injecting a key in open mode is a harmless no-op. Roll out, confirm the cockpit
   is unaffected.
3. **Stage 2 — enforce.** Set `apiKeys.enabled=true` and
   `config.publicApiUrl=https://cfactory-api.freundcloud.org.uk`. The keystore is
   now enforced; the cockpit still works (nginx is already injecting). Verify the
   `/settings/token` page shows the token + API URL.
4. **Stage 3 — editor host.** Add the cloudflared ingress hostname for the editor:
   ```yaml
   - hostname: cfactory-api.freundcloud.org.uk
     service: http://cfactory.factory.svc.cluster.local:3111
   ```
   and the matching Cloudflare DNS/route.

## Using it from the editor

- **One-click:** in VS Code run *Factory: Connect via Browser* → the browser
  (logged in via SSO) hands the token to the editor through `/connect/vscode`.
- **Manual:** open `https://cfactory.freundcloud.org.uk/settings/token`, copy the
  token, and point the extension's `cfactoryUrl` at the editor host
  (`https://cfactory-api.freundcloud.org.uk`).

## Rolling back

Set `apiKeys.enabled=false` (and `frontend.apiKey.enabled=false`). The keystore
returns to OPEN mode and the cockpit is unchanged. Remove the editor cloudflared
hostname to withdraw the external surface.

---

## Path A — Keycloak OIDC (convenience layer, SSO deployments only)

When the cockpit is already fronted by oauth2-proxy + Keycloak, the editor can
skip API keys entirely and log in with the **same SSO**: it obtains a Keycloak
access token (`Factory: Login`) and sends it as the bearer. This needs **no
CFactory changes** — only that oauth2-proxy accept a valid JWT bearer instead of
forcing an interactive login.

This is optional and Keycloak-specific. Path B above still covers every other
deployment; ship Path A *in addition* if you want the smoother SSO flow.

1. **oauth2-proxy** (`oauth2-proxy-cfactory`): add
   ```
   --skip-jwt-bearer-tokens=true
   --oidc-extra-audience=<extension client id>   # if the editor uses a separate client
   ```
   so a request carrying a Keycloak-issued JWT (audience matching) bypasses the
   login redirect and is passed straight upstream. The CFactory backend stays in
   OPEN mode — oauth2-proxy is the gate.
2. **Keycloak**: give the extension a **public client** (PKCE, no secret) with the
   editor redirect URIs registered (`vscode://olafkfreund.factory-vscode/*` and the
   `https://*.vscode.dev/*` external-URI forms for remote/web editors). Map its
   audience to include the oauth2-proxy client so `--skip-jwt-bearer-tokens`
   accepts the token.
3. **Editor settings**:
   ```jsonc
   "factory.cfactoryUrl": "https://cfactory.freundcloud.org.uk",
   "factory.keycloak.issuerUrl": "https://keycloak.freundcloud.org.uk/realms/factory",
   "factory.keycloak.clientId": "<extension public client>"
   ```
   then run **Factory: Login**. The extension auto-refreshes the token; no API key
   or copy-paste involved.

### A vs B

- **A** reuses SSO identity (better audit, no key handling) but only works under
  Keycloak and grants whatever the cockpit grants (no read/write scope split).
- **B** works anywhere and supports scoped read-only vs read-write keys, at the
  cost of the keystore rollout + an editor host.

They are not exclusive — the extension prefers OIDC and falls back to a stored
token, so enabling both gives every user a working path.

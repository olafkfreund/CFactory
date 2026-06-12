# Connecting Editors & External Clients to CFactory

The `factory-vscode` extension (and any external client) talks to CFactory's API
with `Authorization: Bearer <token>`. This guide covers **how a user connects**
and **how an operator enables it** — in every deployment scenario, with or
without SSO.

---

## Quickstart — connect VS Code (users)

> For the hosted deployment at `cfactory.freundcloud.org.uk`.

1. **Get your token.** Open **<https://cfactory.freundcloud.org.uk/settings/token>**
   (log in if prompted) and copy the token with the **Copy** button. The page also
   shows the **API URL** to use.
2. **Configure the extension** (VS Code → Settings, or `settings.json`):
   ```jsonc
   "factory.cfactoryUrl": "https://cfactory-mcp.freundcloud.org.uk",
   "factory.cfactoryToken": "<paste the token>"
   ```
   Or run **Factory: Set CFactory Token** and paste it.
3. **Connect.** The status bar should go green and the pipeline view populates.

> ⚠️ **Use the API URL from the token page, not the cockpit URL.** The cockpit
> (`cfactory.freundcloud.org.uk`) is gated by browser SSO and will *reject* a
> pasted token. The API URL (`cfactory-mcp.freundcloud.org.uk`) goes straight to
> the backend, where your token is the gate.
>
> If you previously set `factory.keycloak.issuerUrl`, clear it — otherwise the
> extension tries SSO login first. With it blank, it uses your pasted token.

That's it. The token doesn't expire; it stays valid until an operator rotates it.

---

## How it works

```
Browser  ──▶ cloudflared ──▶ oauth2-proxy (SSO) ──▶ cockpit nginx ──▶ backend
            cfactory.…                                (injects key)    (keystore)

Editor   ──▶ cloudflared ─────────────────────────────────────────▶ backend
            cfactory-mcp.…   (no SSO proxy; your bearer token is the gate)  (keystore)
```

- The **browser cockpit** is protected by SSO (oauth2-proxy). Inside, the cockpit
  nginx injects the API key for you, so the UI keeps working.
- The **editor** uses a **direct-to-backend host** (here `cfactory-mcp…`, which
  also serves the read-only MCP endpoint) and presents the **token** itself;
  CFactory's keystore validates it.
- In **open mode** (no keys configured — the local/dev default) none of this is
  enforced: the editor connects with no token at all.

## Scenario coverage

| Deployment | Front-door auth | How the editor connects |
|---|---|---|
| **Local / dev** | none (open) | No token needed — the extension sends no `Authorization`; the open API accepts it. |
| **Self-hosted, no SSO** | CFactory API key | The Quickstart above — enable the keystore, paste the token from `/settings/token`. |
| **SSO (Keycloak/oauth2-proxy)** | proxy | Either the API key (Quickstart), **or** the optional OIDC layer below. |
| **Other proxy** (Authelia, basic-auth, mTLS) | proxy-specific | API key on a direct-to-backend host, or open + a network ACL. |

The **API key** is the universal path — it depends on nothing but CFactory.
Keycloak OIDC is an optional convenience that only applies under Keycloak.

---

## Operator runbook — enabling the API key (Path B)

What makes the Quickstart work. The danger is enforcing the keystore *before* the
cockpit nginx injects the key, which would 401 the UI — so do it in two stages.

### 0. Pick the editor host
The editor needs a hostname that reaches the backend **without** going through the
SSO proxy. On this deployment we reuse the existing `cfactory-mcp.freundcloud.org.uk`
cloudflared host (→ `cfactory:3111`); the keystore middleware leaves `/mcp` exempt,
so the MCP server and the editor API coexist. Any direct-to-backend host works.

### 1. Create the Secret
One key, two forms — the scoped string for the backend, the bare key for nginx:
```
KEY="cfk_$(python3 -c 'import secrets;print(secrets.token_hex(24))')"
kubectl -n factory create secret generic cfactory-api-keys \
  --from-literal=api-keys="${KEY}:read,write" \
  --from-literal=api-key="${KEY}"
```
(For a read-only editor token, use `:read` instead of `:read,write`.)

### 2. Stage 1 — inject, keystore still open
Give the **frontend** the bare key so nginx injects it. Helm:
`frontend.apiKey.enabled=true`. Raw manifest — add to the frontend container env:
```yaml
- { name: CFACTORY_API_KEY, valueFrom: { secretKeyRef: { name: cfactory-api-keys, key: api-key } } }
```
Roll out and confirm the cockpit is unaffected (injecting a key in open mode is a
no-op).

### 3. Stage 2 — enforce
Give the **backend** the keystore + the public API URL. Helm: `apiKeys.enabled=true`
and `config.publicApiUrl=https://cfactory-mcp.freundcloud.org.uk`. Raw manifest —
add to the backend container env:
```yaml
- { name: CFACTORY_API_KEYS, valueFrom: { secretKeyRef: { name: cfactory-api-keys, key: api-keys } } }
- { name: CFACTORY_PUBLIC_API_URL, value: "https://cfactory-mcp.freundcloud.org.uk" }
```
Roll out. The cockpit still works (nginx injects the key); `/settings/token` now
shows the token + URL.

### 4. Verify
```
# cockpit through nginx → 200    | editor host without key → 401, with key → 200
curl -so /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $KEY" \
  https://cfactory-mcp.freundcloud.org.uk/api/workitems   # 200
curl -so /dev/null -w '%{http_code}\n' \
  https://cfactory-mcp.freundcloud.org.uk/api/workitems   # 401
```

### What stays open
The keystore middleware enforces `/api/*` and `/connect/*` only. Exempt: `/health`
(k8s probes), `/mcp` (its own `CFACTORY_MCP_SECRET`), `/api/events*` (the idempotent
inbound webhook from the sibling factories). Write endpoints additionally require a
`write`-scoped key.

### Rolling back
Set `apiKeys.enabled=false` (and `frontend.apiKey.enabled=false`), or remove the two
env vars. The keystore returns to OPEN mode and the cockpit is unchanged. Rotating
the key = update the Secret and restart both deployments.

---

## Operator runbook — optional Keycloak OIDC layer (Path A)

Where the cockpit is fronted by oauth2-proxy + Keycloak, a user can skip the API
key and log in with the **same SSO**: the editor obtains a Keycloak access token
(`Factory: Login`) and sends it as the bearer. Needs **no CFactory changes** — only
that oauth2-proxy accept a JWT bearer.

1. **oauth2-proxy** (`oauth2-proxy-cfactory`): add
   `--skip-jwt-bearer-tokens=true` and `--oidc-extra-audience=<editor client id>`,
   so a valid Keycloak JWT bypasses the interactive login.
2. **Keycloak**: create a **public PKCE client** (e.g. `factory-vscode`) with the
   loopback redirect URIs the extension uses (`http://localhost/*`,
   `http://127.0.0.1/*`, `http://localhost:*/callback`) and an audience mapper
   adding the oauth2-proxy client id to the token `aud`.
3. **Editor settings**:
   ```jsonc
   "factory.cfactoryUrl": "https://cfactory.freundcloud.org.uk",
   "factory.keycloak.issuerUrl": "https://keycloak.freundcloud.org.uk/realms/factory",
   "factory.keycloak.clientId": "factory-vscode"
   ```
   then run **Factory: Login**. The token auto-refreshes; no key handling.

### A vs B
- **A** reuses SSO identity (auto-refresh, no key to manage) but only works under
  Keycloak and grants whatever the cockpit grants (no read/write scope split).
- **B** works anywhere and supports scoped read-only vs read-write keys.

They are not exclusive — the extension prefers OIDC and falls back to a stored
token, so both can be enabled at once.

---

## Reference

**Endpoints**
- `GET /settings/token` — the copy page (also `GET /api/settings/token` → `{token, configured, connect_url}`).
- `GET /connect/vscode?redirect=<editor cb>&state=<nonce>` — one-click hand-off; 302s to `<redirect>?token=…&state=…`.

**Settings** (`CFACTORY_*` env / Helm `config.*` + `apiKeys.*` + `frontend.apiKey.*`)
- `CFACTORY_API_KEYS` — `"<key>:read,write;<key2>:read"`; empty = OPEN mode.
- `CFACTORY_API_KEY` (frontend) — the bare key nginx injects.
- `CFACTORY_PUBLIC_API_URL` — shown on `/settings/token` as the editor API URL.

**Extension settings**
- `factory.cfactoryUrl` — the API base URL (the direct-to-backend host).
- `factory.cfactoryToken` — pasted token (or use **Factory: Set CFactory Token**).
- `factory.keycloak.issuerUrl` / `factory.keycloak.clientId` — OIDC path only; leave blank to use a pasted token.

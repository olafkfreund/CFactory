# CFactory: Local → Hosted Multi-Tenant Runbook

CFactory ships **local-first**: single user, single tenant, auth OPEN. This
guide is the operator "flip" runbook for promoting a local instance to a hosted,
shared, multi-tenant deployment. It ties together the enterprise seams added in
#20 (scoped API keys), #21 (audit HMAC chain + identity), and #23 (multi-tenant
flag + tenant resolution), and the Helm chart from #22 (`charts/cfactory`).

Each step is independent and additive — you can enable them one at a time. All
configuration is `CFACTORY_*` environment variables (see
`apps/backend/cfactory/config.py`), surfaced in the chart under `config.*`,
`apiKeys.*`, and `database.*`.

## What is enforced today vs deferred

**Enforced today (this is real, working behaviour):**

- **Scoped API-key auth (#20).** When `CFACTORY_API_KEYS` is set, every request
  must carry a known key with the required scope (`read` / `write`). When unset,
  auth is OPEN (local single-user mode).
- **Caller identity → audit actor (#21).** The presented API key (or `local` in
  OPEN mode) is stamped as the `actor` on each audit entry.
- **Tamper-evident audit chain (#21).** Audit entries are HMAC-SHA256 chained
  under `CFACTORY_AUDIT_HMAC_SECRET`; any after-the-fact mutation breaks the
  chain.
- **Tenant resolution seam + flag (#23).** With `CFACTORY_MULTI_TENANT=true`,
  the tenant is resolved per request from the `X-Tenant-Id` header (falling back
  to `default`). `/health` reports the active mode.

**Deferred to the hosted offering (NOT enforced yet — documented seams only):**

- **Per-tenant data isolation.** The resolved tenant is *not* yet threaded into
  store/audit queries; all data is still effectively single-tenant. The
  `tenant_id_for` resolver (`apps/backend/cfactory/enterprise.py`) is the single
  hook where query scoping will be wired in. See the module docstring.
- **Full SAML / SCIM IdP integration.** Real SSO login, SCIM user/group
  provisioning, and group→scope mapping are provided by AIFactory's enterprise
  auth modules. The `AuthProvider` Protocol in `enterprise.py` is the contract a
  hosted provider implements; local v1 ships `LocalAuthProvider` (API-key
  backed). See "SAML/SCIM plug point" below.

## Step 1 — Turn on scoped API keys (#20)

Set `CFACTORY_API_KEYS` to a `;`-separated list of `<key>:<scopes>` entries:

```bash
export CFACTORY_API_KEYS="ops-rw:read,write;dash-ro:read"
```

Callers then authenticate with `Authorization: Bearer <key>` (or `X-API-Key`).
Read-only callers get `read`; mutating callers need `write`. With no keys set,
the instance stays OPEN — fine for local, **not** for hosted.

In Helm: store the string in a Secret and enable it:

```yaml
apiKeys:
  enabled: true
  existingSecret: cfactory-api-keys   # Secret carrying the keys string
  secretKey: api-keys
```

## Step 2 — Set a real audit HMAC secret and plan rotation (#21)

The default `CFACTORY_AUDIT_HMAC_SECRET` is a clearly-labelled dev secret. In
any shared deployment, set a real one:

```bash
export CFACTORY_AUDIT_HMAC_SECRET="$(openssl rand -hex 32)"
```

**Rotation.** The audit chain is HMAC-anchored to this secret. Rotating the
secret starts a *new* chain segment from the rotation point forward; entries
written under the old secret remain verifiable only with the old secret.
Operational guidance:

1. Record the rotation time and the previous secret (keep it for re-verifying
   historical entries).
2. Roll out the new secret (update the Secret, restart pods).
3. Verify the chain from the rotation point forward under the new secret.

Inject it in Helm via `config.extraEnv` referencing a Secret (do not put it in
the ConfigMap):

```yaml
config:
  extraEnv:
    - name: CFACTORY_AUDIT_HMAC_SECRET
      valueFrom:
        secretKeyRef: { name: cfactory-audit, key: hmac-secret }
```

## Step 3 — Flip on multi-tenant mode (#23)

```bash
export CFACTORY_MULTI_TENANT=true
```

With the flag **off** (default), `tenant_id_for(request)` always returns
`default` — unchanged local behaviour. With it **on**, the tenant is resolved
from the `X-Tenant-Id` request header, falling back to `default` when the header
is absent or blank. Confirm the mode via `/health`:

```bash
curl -s localhost:3111/health | jq '.multi_tenant'   # true
```

Callers (the cockpit gateway / per-tenant proxy) attach the header:

```
X-Tenant-Id: acme
```

> **Reminder:** enabling this flag turns on *resolution*, not *isolation*.
> Per-tenant query scoping is deferred (see "What is enforced today"). Do not
> treat this flag alone as a data-isolation boundary.

In Helm (default false, ConfigMap-injected `CFACTORY_MULTI_TENANT`):

```yaml
config:
  multiTenant: true
```

## SAML / SCIM plug point

For hosted SSO, supply an `AuthProvider` implementation (the Protocol in
`apps/backend/cfactory/enterprise.py`) backed by AIFactory's SAML/SCIM modules:

- `authenticate(request) -> str` resolves the federated principal (SAML
  assertion / session) instead of an API key.
- `scopes_for(identity) -> set[str]` derives scopes from SCIM group membership
  instead of the local key store.

The `identity_dep` FastAPI dependency is the wholesale-overridable seam (it is
already overridden in tests to inject e.g. `saml|alice@corp`). A hosted
deployment replaces `LocalAuthProvider` / `identity_dep` and the `tenant_id_for`
resolver; no rewrite of the surrounding code is required.

## Hosted deploy with Helm

Reference chart: `charts/cfactory` (see `guides/deployment.md` for build/run
basics). A hosted values overlay typically combines all of the above:

```yaml
config:
  aifactoryApiUrl: "http://aifactory.ai.svc.cluster.local:3101"
  pfactoryApiUrl: "http://pfactory.ai.svc.cluster.local:3105"
  tfactoryApiUrl: "http://tfactory.ai.svc.cluster.local:3103"
  multiTenant: true
  extraEnv:
    - name: CFACTORY_AUDIT_HMAC_SECRET
      valueFrom:
        secretKeyRef: { name: cfactory-audit, key: hmac-secret }

apiKeys:
  enabled: true
  existingSecret: cfactory-api-keys
  secretKey: api-keys

database:
  enabled: true
  existingSecret: cfactory-db
  secretKey: database-url
```

Plain (non-secret) `config.*` values render into a ConfigMap and are injected
via `envFrom`; `apiKeys` and `database` come from Secrets. After applying, check
`/health` reports `"multi_tenant": true` and that authenticated, correctly
scoped requests succeed while unscoped ones are rejected.

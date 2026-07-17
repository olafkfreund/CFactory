# Multi-Tenant Mode

CFactory can partition work items and the cockpit APIs by tenant. The feature is
gated behind a single flag and is **off by default**: CFactory is local-first,
and a single-operator deployment should never pay for (or misconfigure) tenancy
it does not have.

## The flag

**`CFACTORY_MULTI_TENANT`** (Helm `config.multiTenant`, default `false`)

| | Off (default) | On |
|---|---|---|
| Tenant resolution | Always the single implicit `default` tenant; headers are ignored | Resolved per request from `X-Tenant-Id`, falling back to `default` when absent/blank |
| Reads (`/api/workitems`, cockpit APIs) | Unscoped — exactly the pre-#172 behaviour | Filtered to the resolved tenant |
| Writes | Stamped `default` | Stamped with the resolved tenant |

## `X-Tenant-Id` resolution

CFactory never trusts the browser for tenancy. The header is injected by the
infrastructure in front of it:

1. The user's **Keycloak group** maps to a `tenant` claim on their access token.
2. **oauth2-proxy** (alpha-config) injects that claim as an `X-Tenant-Id`
   request header on every proxied request (see factory-gitops#13).
3. In multi-tenant mode the backend resolves the tenant from that header;
   absent or blank resolves to `default`.

With the flag off, the header — whatever a client sends — is ignored entirely.

## Schema migration

The `work_items.tenant_id` column arrives by either of two equivalent paths:

- **Alembic**: revision `a7c3f2e19b40` (for deployments that run migrations).
- **Init-time guard**: on startup the store inspects the live schema and adds
  the column (backfilled to `default`, plus its index) if missing — so a
  deployed sqlite upgrades itself on the next restart.

Both are idempotent; existing rows land in the `default` tenant either way, so
flipping the flag later does not orphan pre-existing data for the first tenant.

## Operator flip steps

1. **Keycloak**: put users in per-tenant groups and add a mapper that emits the
   group as a `tenant` claim on the access token.
2. **oauth2-proxy**: switch to alpha-config with a claim-to-header injection of
   `tenant` as `X-Tenant-Id` (factory-gitops#13 has the working config).
3. **CFactory backend**: set `CFACTORY_MULTI_TENANT=true` (Helm
   `config.multiTenant=true`) and roll out. The schema guard applies the
   `tenant_id` column on startup if the alembic revision has not run.
4. **Verify**: requests carrying different `X-Tenant-Id` values see disjoint
   work-item lists; requests without the header see the `default` tenant.

Rolling back is the reverse of step 3 only: unset the flag and behaviour
returns to single-tenant. The column stays (harmlessly) in place.

## Known ceilings

Documented, deliberate, and tracked for when a second tenant onboards:

- **WebSocket fan-out is fleet-wide.** Live events reach every connected
  cockpit regardless of tenant; the partition holds for the REST surface only.
- **Correlation keys are globally unique across tenants.** A cross-tenant key
  collision on write is rejected (IntegrityError), not namespaced; the upgrade
  path is a composite `(tenant_id, correlation_key)` unique constraint.

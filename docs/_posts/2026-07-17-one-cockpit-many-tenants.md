---
layout: post
title: "One cockpit, many tenants"
subtitle: "Work items and the cockpit APIs are now tenant-scoped behind a flag, the identity layer that feeds them is wired end to end, and fixed issues finally close themselves."
date: 2026-07-17 12:00:00
author: Factory Team
---

CFactory has always been local-first: one operator, one store, one cockpit. That
stays true by default. But the hosted deployment shares one cockpit across
whoever Keycloak lets in, and "everyone sees everything" is not a property you
want to discover after the second team onboards. This cycle put the partition in
place before it is needed.

## Tenant scoping behind a flag

Work items and the cockpit APIs are now tenant-scoped when
`CFACTORY_MULTI_TENANT` is on (#172). The mechanics are deliberately small: the
store grows a `tenant_id` column, and every route resolves its store through a
dependency that returns a scoped view — reads filter by the resolved tenant,
writes stamp it. With the flag off (the default), resolution always yields the
single implicit `default` tenant and behaviour is byte-for-byte what it was
before.

The migration is dual-path on purpose. There is a proper alembic revision for
deployments that run migrations, and an idempotent init-time guard that inspects
the live schema and adds the column with a `default` backfill if it is missing —
so the deployed sqlite upgrades itself on the next restart without anyone
running a migration job. Both paths converge on the same schema; running both is
harmless.

## Where the tenant comes from

The scoping is only as good as the identity behind it, so the resolution chain
is explicit: a user's Keycloak group maps to a `tenant` claim on their token,
and oauth2-proxy's alpha-config injects that claim as an `X-Tenant-Id` header on
every proxied request (factory-gitops#13). CFactory never trusts the browser for
tenancy — the header is set by the proxy in front of it, after authentication.
In multi-tenant mode the backend reads that header; absent or blank, it falls
back to `default` rather than failing closed on day one.

## Issues that close themselves

Smaller but overdue: the auto-close workflow is live on main. Fixes merged to
the default branch now close the issues they reference, instead of leaving a
trail of solved-but-open issues that only get discovered during audits. GitHub
only fires closing keywords on the default branch, which is exactly where our
dev-branch flow kept dropping them.

## The honest ceilings

Two things are documented rather than solved. WebSocket fan-out is still
fleet-wide: live events reach every connected cockpit regardless of tenant, so
the partition currently holds for the REST surface, not the event stream. And
correlation keys remain globally unique across tenants — a cross-tenant key
collision would be rejected, not namespaced. Both are fine with one real tenant
and both have a clear upgrade path (per-tenant fan-out filtering, a composite
tenant-plus-key uniqueness constraint) that we will take when a second tenant
actually onboards, not before.

## What is next

Flip the flag on the hosted deployment once the gitops side is rolled out,
then close the two ceilings in the order a second tenant would hit them:
event-stream filtering first, key namespacing second.

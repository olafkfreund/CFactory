---
status: approved
issue: 517
intent: intent/2026-11-16-review-low-findings-cfactory.md
---

# Spec: tenant-bound extra org sockets

## Design

- `Settings.extra_org_bindings_for(service) -> list[tuple[str | None, str]]`: each entry of
  `*_ORG_TOKENS` is `key` or `tenant=key` (split on the first `=`). `extra_tokens_for` returns the
  keys only, so reconcile is unchanged.
- `start_subscribers` passes the tenant to `subscribe` -> `handle_message(..., tenant=None)`.
- In `handle_message`, when `tenant` is set AND `settings.multi_tenant`: upsert through
  `store.scoped(tenant)`, card write-back through `get_cards_store().scoped(tenant)`, and
  `manager.broadcast(msg, tenant=tenant)`. Otherwise unchanged.
- `ConnectionManager.connect(ws, tenant=None)` records the socket's tenant;
  `broadcast(msg, tenant=None)` sends a tenant message only to sockets of that tenant (a `None`
  message goes to all). `routes_ws` passes `resolve_tenant(x-tenant-id)` when multi-tenant.

## Alternatives rejected

- Deriving the org from the key: the key is opaque to CFactory.
- Scoping REST/poll broadcasts too: wider blast radius; follow-up.

## Risks

Codex review (confirmed, carried as scope limits): (1) the poll-loop and `/api/refresh` snapshot broadcasts still send the whole unscoped store to every client, so the leak is closed for relayed events only and the snapshot path is a follow-up (owner decision: it needs tenant on WorkItem payloads). (2) Binding a key whose correlation keys already exist under `default` makes the scoped upsert hit the globally unique key; bind keys at first use, or migrate rows. Both are documented in the env reference.

A key with `=` in its first segment is read as a binding; documented, bare keys are the default.

## Verification

Tests: binding parse; scoped store + tenant broadcast; other tenant's client gets nothing; no
binding or multi-tenant off = old behaviour.

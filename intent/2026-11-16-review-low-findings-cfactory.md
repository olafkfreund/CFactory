---
status: draft
issue: 517
---

# Intent: events from extra org-bound sockets must stay in their org

## Problem

`upstream_ws.handle_message` writes every relayed event to the unscoped store and
`manager.broadcast`s it to every cockpit socket. With `CFACTORY_<SVC>_ORG_TOKENS` (Factory#3702)
and multi-tenant mode on, an org-B event is stored under the default tenant and pushed to
tenant-A cockpits. Org binding is lost on the CFactory side. (Review wave 8, LOW.)

## Proposed outcome

An extra org key may be bound to a tenant (`tenant=key`). Events from that socket are stored and
broadcast for that tenant only. Bare keys and multi-tenant OFF behave exactly as today.

## Affected

`upstream_ws.py`, `config.py`, `ws.py`, `routes_ws.py`, the env reference.

## Constraints

No change without bindings. Tokens never logged. REST-ingress broadcast and the poll loop stay
unscoped (out of scope, listed as a follow-up).

## Open questions

None: the tenant binding syntax is the only new config.

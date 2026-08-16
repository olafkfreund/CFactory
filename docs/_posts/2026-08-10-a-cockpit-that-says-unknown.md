---
layout: post
title: "A cockpit that says unknown"
subtitle: "Three weeks on the control plane: multi-tenant git connections with an encrypted per-tenant credential store, an audit chain that distinguishes a write race from tampering, and a rule that a stage we cannot identify is rendered as unknown rather than as a plausible guess."
date: 2026-08-10 09:00:00
author: Factory Team
---

CFactory is where a person watches the line. That makes its failure mode
different from the rest of the fleet: the cockpit does not break builds, it
misinforms the human deciding whether to intervene. A wrong number on a dashboard
is worse than a missing one, because a missing one prompts a question.

Most of the last three weeks went on multi-tenant git support and on the several
places the cockpit was displaying a confident answer it had not earned.

## Bring your own git host

The largest body of work is tenant git configuration: the cockpit can now manage
**multiple git connections and repositories per tenant**, installed through a
GitHub App or GitLab OAuth flow rather than a pasted token, with credentials held
in an **encrypted per-tenant store**. The selected provider propagates across the
fleet, so the planner reconnoitres the tenant's actual host, the builder pushes
to it, and the verifier posts its verdict back to it.

The board grew to match: connected repositories are polled so cards stay in sync
without a manual refresh, and issue comments are imported and rendered on the card
rather than requiring a trip to the git host to read the conversation.

The important design decision is that a tenant's credential is never a global.
The previous model had one shared token doing everything, which is convenient
right up until it is the entire blast radius.

## The cockpit stopped guessing

Four fixes this month were the same instinct applied to the interface.

An **unrecognised stage key used to blank the whole process pane**. One unexpected
value from the backend, and the operator lost the entire view rather than one
field. Worse, an unreachable stage was being rendered as an *earlier* stage —
which is not a missing answer, it is a wrong one that looks plausible and tells
someone the run is further back than it is. Both now render as **unknown**, which
is a true statement about our knowledge.

A **poll was erasing usage data that only an event can carry**. Token and cost
figures arrive on the event stream; the periodic poll returned a payload without
them and overwrote what the events had delivered. The dashboard did not go blank —
it went to zero, which reads as "this run was free".

And the **test graph was being fetched from a route that does not carry lane
information**, so the graph rendered without the lanes it exists to show.

## An audit trail that admits what it cannot prove

The audit chain is tamper-evident: each entry hashes the one before it, so an
alteration breaks the chain. Two changes made it usable rather than merely
correct.

First, the chain verdict is now shown, and it **distinguishes a known fork from a
new one**. A break that has already been investigated and explained is a different
thing from one that appeared this morning, and collapsing them means the alert is
either permanently red or permanently ignored.

Second, and more subtly: concurrent appends could interleave and produce a broken
chain that looked exactly like tampering. Appends are now serialised, and the
system can **tell a write race from a tamper** rather than reporting an
infrastructure bug as a security event. Crying wolf about tampering is how a real
tamper gets triaged as noise.

The trail also now says when it **cannot name a person**. Actions taken through
automation are attributable to a service, not a human, and the previous display
implied a person had confirmed something when the evidence did not support that.
The confirmation is now shown on proof rather than on a header a caller can set.

## Contracts, checked rather than described

The cockpit publishes an API that other services and the hub depend on. Three
fixes this month were about that contract being real:

- `openapi.yaml` was not a valid OpenAPI 3.0.3 document. It is now, and a check
  asserts it stays that way.
- Three hand-written statements of the card shape disagreed with what the service
  actually serves. They now agree, and the hub compares them on every pull
  request.
- A card body that is unknown or empty is **rejected rather than discarded**, and
  a blank acceptance criterion is refused on write. Silently dropping a field the
  caller sent is the worst of both options: the caller believes it was stored.

The card schema is now the checked source of truth, gated from the hub, rather
than a document that four codebases each remembered differently.

## Housekeeping worth naming

The runtime image no longer ships `pip`, which cleared two published
vulnerabilities — a build tool in a runtime image is attack surface with no
runtime purpose. Database migrations now run at startup and adopt the schema that
an earlier `create_all` had already produced, so a fresh deployment and an
upgraded one converge instead of diverging. The lint ratchet was brought under
the gate it implements, having previously been exempt from its own rule. And the
startup probe stopped blocking startup for twenty seconds waiting on the tracing
collector, which is an availability regression introduced by an observability
feature.

## The theme

Everything above is one preference stated four ways: **a dashboard should never
present a guess with the same confidence as a measurement.** Unknown is a
legitimate answer, zero is not a synonym for absent, and an earlier stage is not a
safe stand-in for one we could not identify.

The cockpit is the only part of the Factory a person reads directly. It should be
the part least willing to make something up.

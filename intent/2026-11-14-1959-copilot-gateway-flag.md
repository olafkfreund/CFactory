---
status: draft
issue: 1959
repo-issue: olafkfreund/Factory#1959
author: olafkfreund
---

# Intent: copilot can call the model gateway, behind a flag that defaults off

## Problem

Factory#1959 moves model calls behind the in-house model gateway so that no
service holds a provider key. The gateway runs in the cluster (namespace
`gateway`, `http://model-gateway.gateway.svc.cluster.local:8080`) but no
service can use it: CFactory's copilot, the epic's stage 1, always sends its
OpenAI-compatible calls straight to `ollama_cloud_base_url` with
`OLLAMA_API_KEY` (`apps/backend/cfactory/copilot/service.py`, `make_runner`).

The gateway is not configured yet (the owner still has to seed its Secret and
decide its egress), so nothing may switch to it now. But the service-side
change does not depend on those decisions and can ship dark.

## Proposed outcome

- A new setting names the gateway URL. Unset (the default), CFactory behaves
  exactly as today.
- When it is set and the copilot provider is OpenAI-compatible, the copilot's
  chat call goes to the gateway and carries a gateway client token, not
  `OLLAMA_API_KEY`. The provider key is not sent anywhere.
- The provider status endpoint says the copilot is routed through the gateway
  and does not send `OLLAMA_API_KEY` to it.
- The Claude provider (the default, Plane B in the epic) is unchanged.

## Affected users and systems

- CFactory backend: `config.py`, `copilot/service.py`, their tests.
- Operators: one new opt-in setting pair. No deployment change in this task;
  the gitops side is `factory-gitops` slug `2026-11-14-1959-gateway-stage1-secretref`.
- No effect on PFactory, AIFactory or TFactory.

## Constraints

- Default off: with the setting unset there is no behaviour change and no new
  network call.
- When the flag is on, never fall back to the direct provider call: a gateway
  failure is an error, not a silent bypass.
- The gateway only serves `POST /v1/chat/completions` (buffered, no streaming)
  and `GET /healthz`; it has no `/v1/models`.
- No secrets in the repo. No deployment, kubectl or Secret change.
- Removing `OLLAMA_API_KEY` from the CFactory Deployment is a later step of
  the epic, after the gateway path is proven live. Not in this task.

## Open questions

None for this task. The gateway Secret and egress decisions belong to the
owner and are tracked on Factory#1959; this change does not need them.

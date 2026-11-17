---
status: approved
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
The provider status probe (`provider_status`, same file) also sends that key
to `{base}/models`, and it does so even when the provider is `claude`, as
long as a key is set.

The gateway is not configured yet (the owner still has to seed its Secret and
decide its egress), so nothing may switch to it now. But the service-side
change does not depend on those decisions and can ship dark.

## Proposed outcome

- A new setting names the gateway URL. Unset (the default), CFactory behaves
  exactly as today.
- When it is set and the copilot provider is OpenAI-compatible, the copilot's
  chat call goes to the gateway and carries a gateway client token, not
  `OLLAMA_API_KEY`. The provider key is not sent anywhere.
- With the setting on, CFactory sends `OLLAMA_API_KEY` to no host at all:
  the status probe stops calling the provider too, whichever provider is
  selected. Status says the copilot is routed through the gateway and whether
  the gateway answers its health check. That is reachability only; it does
  not prove a chat call would succeed, and there is no model list.
- Chat with the Claude provider (the default, Plane B in the epic) is
  unchanged.

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
- The setting is the gateway origin, with no `/v1` suffix; CFactory adds the
  path. This differs from `ollama_cloud_base_url`, which includes `/v1`.
- The client token is a credential: `repr=False` like every other credential
  in `Settings`, never in a status response or a log line. URL set with no
  token is an error on the chat call, not an unauthenticated request.
- Status keeps reporting the selected `provider` value unchanged, so a client
  that sends it back to the settings endpoint still validates.
- The gateway only serves `POST /v1/chat/completions` (buffered, no streaming)
  and `GET /healthz`; it has no `/v1/models`.
- No secrets in the repo. No deployment, kubectl or Secret change.
- Removing `OLLAMA_API_KEY` from the CFactory Deployment is a later step of
  the epic, after the gateway path is proven live. Not in this task.

## Open questions

None for this task. Turning the flag on later needs owner actions that this
change does not: the gateway Secret in `gateway`, the same client token in a
Secret in `factory` for CFactory, the gateway's egress, and a manifest change
that sets the two CFactory settings. They are tracked on Factory#1959.

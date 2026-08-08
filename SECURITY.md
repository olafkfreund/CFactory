# Security Policy

CFactory is a public repository. As of this writing it carries open
code-scanning alerts and at least one open Dependabot advisory on its
default branch (`dev`) — check the
[Security tab](https://github.com/olafkfreund/CFactory/security) for the
current state rather than trusting a number in this file.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

The intended channel is GitHub's private vulnerability reporting:

> https://github.com/olafkfreund/CFactory/security/advisories/new

That feature is **not yet enabled on this repository** (tracked in
[#344](https://github.com/olafkfreund/CFactory/issues/344)). Until it is
turned on, the link above will not accept a report. In the meantime:

- If the issue is not urgent and does not need to stay private, open a
  regular issue with as few exploit specifics as you're comfortable
  including.
- If it does need to stay private, contact the maintainer,
  [@olafkfreund](https://github.com/olafkfreund), directly on GitHub and
  ask that #344 be resolved first.

There is no dedicated security email for this project. Do not send reports
to an address you find elsewhere and assume it reaches a security team —
none exists; CFactory has one maintainer.

Include, where possible:

- A description of the issue and its impact
- Steps to reproduce (PoC welcome)
- Affected version / commit SHA
- Suggested mitigation, if any

## Response targets

This is a solo-maintained project with no on-call rotation, so treat these
as targets, not a contractual SLA:

| Stage              | Target                  |
|--------------------|-------------------------|
| Acknowledgement    | within 5 business days  |
| Triage + severity  | within 10 business days |
| Fix or mitigation  | depends on severity     |
| Public disclosure  | coordinated with reporter, typically after a fix ships |

## Supported versions

CFactory has no tagged releases (see [RELEASE.md](RELEASE.md)): `main` is
what deploys, and only the code currently on `main` is supported. There is
no backport policy for older commits.

## Scope

In scope:

- The CFactory backend (`apps/backend/`) and cockpit frontend
  (`apps/frontend-web/`) in this repository
- The container images built from `Dockerfile` and
  `apps/frontend-web/Dockerfile`
- The Helm chart in `charts/cfactory/`

Out of scope:

- The upstream services CFactory observes (PFactory, AIFactory, TFactory) —
  report to their own repositories
- Third-party providers (Anthropic, GitHub, Keycloak) — report to those
  vendors directly
- Vulnerabilities that require local root or physical access to the
  deployment cluster
- Self-inflicted issues from disabling auth, weakening CORS, or bypassing
  the human-in-the-loop approval gate outside the documented flow

## Safe harbor

Good-faith research conducted under this policy will not be subject to
legal action by the maintainer. Please act in good faith: avoid privacy
violations, data destruction, or service degradation, and give a reasonable
window to fix an issue before any public disclosure.

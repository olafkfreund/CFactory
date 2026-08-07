---
layout: default
title: Environment reference
permalink: /dev/environment-reference/
---

# Environment reference

This is the complete reference for every environment variable, flag and
operational parameter CFactory reads — backend, the copilot's external secret,
the frontend build-time bundle, and the frontend container runtime. A fresh
operator needs nothing beyond this page plus the [`.env.example`](https://github.com/olafkfreund/CFactory/blob/main/.env.example)
at the repo root.

Almost every backend value is a field on the `Settings` class in
`apps/backend/cfactory/config.py` (`env_prefix="CFACTORY_"`, `.env` file
support), with one exception — `CFACTORY_STALL_DEADLINE_SECONDS` is read
directly in `store.py`. Two backend values also accept a bare, un-prefixed
alias so they can share the fleet-wide Ollama secret.

Conventions in the tables below:

- **Required?** — `no` means the built-in default is fine everywhere; *in hosted*
  means it must be set for any shared/multi-user deployment; *if provider is X*
  means it is only needed for a particular copilot provider.
- **Server-side only** secrets are never sent to the browser.

## Backend (`CFACTORY_*`)

| Variable | Default | Required? | Purpose |
|---|---|---|---|
| `CFACTORY_BACKEND_PORT` | `3111` | no | API server port. |
| `CFACTORY_FRONTEND_PORT` | `3110` | no | Cockpit UI port / CORS origin. |
| `CFACTORY_WORKSPACE_ROOT` | `~/.cfactory` | no | Local state root — persisted endpoint/copilot override JSON and the local SQLite store. |
| `CFACTORY_AIFACTORY_API_URL` | `http://localhost:3101` | no | AIFactory (Act) upstream REST base. Editable at runtime from the Services view. |
| `CFACTORY_PFACTORY_API_URL` | `http://localhost:3105` | no | PFactory (Plan) upstream REST base. Editable at runtime. |
| `CFACTORY_TFACTORY_API_URL` | `http://localhost:3103` | no | TFactory (Verify) upstream REST base. Editable at runtime. |
| `CFACTORY_OBSERVE_API_URL` | `http://localhost:5080` | no | OpenObserve health-probe base for the Services reachability view (not a PARR factory; never polled/hydrated). In-cluster: `http://observe.factory.svc.cluster.local:5080`. |
| `CFACTORY_UPSTREAM_TOKEN` | _(unset)_ | in hosted | Shared bearer token sent as `Authorization: Bearer <token>` on every adapter call, live-progress poll and upstream WS subscription. Leave unset only for local dev where the factories run `APP_DISABLE_AUTH=true`. Server-side only. |
| `CFACTORY_AIFACTORY_TOKEN` | _(unset; falls back to `CFACTORY_UPSTREAM_TOKEN`)_ | no | Service token for AIFactory's live-agent console WebSocket. Server-side only. |
| `CFACTORY_INTAKE_PROJECT_ID` | _(unset)_ | **DEPRECATED — a one-release seed** | AIFactory project id a dispatched planning card is built into (RFC-0019 §3.2). **RFC-0020 §3.3 retired it as configuration:** it is now the tenant's `aifactory_project_id`, editable in Settings > Git integration. It survives ONE release as a seed — on first boot a tenant with no stored git config materialises one from it, after which the stored value is authoritative and a restart never overwrites an edit. Removed next release; do not set it on a new deployment. Note it is an AIFactory project id, **not** a repository path. Unset and unconfigured = a `low`/`medium` card promoted to `ready` is moved to `blocked` with that reason (loud, never silently accepted); `hard` cards route to PFactory and need no project. Recommended value and the full story: [the planning-board guide](../guides/planning-board.md#git-integration-the-settings-panel). |
| `CFACTORY_DATABASE_URL` | _(unset)_ | no | Postgres connection string for the WorkItem correlation store. Unset = local SQLite under the workspace root. Also stores the RFC-0019 planning cards, in their own table. |
| `CFACTORY_SUBSCRIBE_UPSTREAMS` | `false` | no | On: connect to each upstream `/ws/events` feed at startup. Off: no upstream WS (avoids reconnect loops against down services). |
| `CFACTORY_LIVE_PROGRESS` | `false` | no | On: poll PFactory/TFactory + subscribe AIFactory progress and broadcast `{type:"progress"}`. Off: no live-progress. |
| `CFACTORY_POLL_INTERVAL_SECONDS` | `3.0` | no | Live-progress poll interval, seconds. Only relevant when `CFACTORY_LIVE_PROGRESS` is on. |
| `CFACTORY_STALL_DEADLINE_SECONDS` | `900.0` | no | Idle budget (seconds) before an active, non-terminal stage is flagged stalled and pruned from the board. Values `<= 0` or unparseable fall back to the default. Read directly in `store.py`. |
| `CFACTORY_COPILOT_MODEL` | `claude-opus-4-8` | no | Copilot model id; meaning depends on `CFACTORY_COPILOT_PROVIDER`. Editable at runtime from the Settings view. |
| `CFACTORY_COPILOT_PROVIDER` | `claude` | no | Copilot LLM provider: `claude` (Claude Agent SDK, reads `ANTHROPIC_API_KEY`) or `ollama`/`openai_compatible` (any OpenAI-compatible chat endpoint). |
| `CFACTORY_OLLAMA_CLOUD_BASE_URL` | `https://ollama.com/v1` | no | OpenAI-compatible copilot base URL (includes `/v1`). Also accepts the bare `OLLAMA_CLOUD_BASE_URL`. Only used when provider is not `claude`. |
| `CFACTORY_OLLAMA_API_KEY` | _(unset)_ | if provider is ollama | Bearer key for the OpenAI-compatible copilot endpoint. Also accepts the bare `OLLAMA_API_KEY` (shared factory secret). Server-side only. |
| `CFACTORY_API_KEYS` | _(unset)_ | in hosted | Scoped API keys `<key>:read,write;<key2>:read`. Unset = auth OPEN (single-user local mode); set = requests must carry a known key with the required scope. Also gates `/mcp` per declared tool scope, so a `read` key can enumerate the planning backlog and never change it (RFC-0019 Phase 2a). Recommended: one `read` key per watcher, one `read,write` key per agent that plans — never one shared key. Server-side only. |
| `CFACTORY_MCP_SECRET` | _(unset)_ | in hosted | LEGACY full-scope bearer for the MCP transport (`POST /mcp`) — a caller presenting it holds `read` and `write`, including every board write tool. Still the supported prod credential; scoped `CFACTORY_API_KEYS` work alongside it and are preferred for new clients. A wrong token against a configured server is a loud 401. Server-side only. |
| `CFACTORY_MCP_DEV_OPEN` | `false` | no | Explicit local-dev opt-in that reopens `/mcp` when NO credential is configured. Unconfigured otherwise means DENY (RFC-0019 Phase 2a) — never set this in a hosted deploy. Ignored once `CFACTORY_MCP_SECRET` or `CFACTORY_API_KEYS` is set. |
| `CFACTORY_PUBLIC_API_URL` | _(unset)_ | no | Public base URL of the token-gated API shown on `/settings/token` for editor/external clients. Display only. |
| `CFACTORY_MULTI_TENANT` | `false` | no | On: resolve tenant per request from the `X-Tenant-Id` header (hosted, injected by oauth2-proxy from the Keycloak tenant claim). Off: single `default` tenant. Planning cards are scoped the same way, and `card_key` is unique per tenant — two tenants may each hold an `FCT-1`. |
| `CFACTORY_OIDC_ISSUER` | _(unset)_ | no | Issuer of the OIDC ID token oauth2-proxy injects in front of the cockpit, e.g. `https://keycloak.example/realms/factory`. **Set** = a confirmed HITL action is attributed in the audit trail to the *person* who confirmed it (`user:<email>`), verified against the issuer's JWKS. **Unset** (local/dev, and any deploy with no IdP) = the actor stays `unattributed:key-<digest>`, an honest reference to the shared API key that acted (#251). Not a secret and grants nothing — authorization remains `CFACTORY_API_KEYS`' job — so a wrong value only drops back to the unattributed actor. |
| `CFACTORY_OIDC_AUDIENCE` | _(unset)_ | no | Expected `aud` on that ID token — the OIDC client id the cockpit's oauth2-proxy is registered as. Unset = accept any token the issuer signed, right when the realm *is* the trust boundary. Set it where the realm also serves clients whose users must not be able to name themselves in this trail. Ignored when `CFACTORY_OIDC_ISSUER` is unset. |
| `CFACTORY_AUDIT_ACKNOWLEDGED_FORKS` | _(unset)_ | no | Audit-entry ids whose chain **fork** this deployment has already explained, comma separated (e.g. `2178`). A fork is two appends that raced on the tail read before #310 serialised them: both rows are genuine, every HMAC in them recomputes, and relinking one is the exact edit the chain exists to detect — so the row is left as written and named here instead. **Set** = `GET /api/audit/chain` and the Audit view count a listed fork and show it, but do not let it colour the verdict; anything **not** listed still does, so a regression of the append serialisation is still loud. **Unset** (the default, and every fresh database) = nothing is acknowledged, which is right: there, any fork at all is news. Only ever forgives a `forked` classification — a `mutated`, `duplicate` or `dangling` entry is reported whatever is listed, so an id here can never buy silence for a later edit to that row. Not a secret. |
| `CFACTORY_GITHUB_TOKEN` | _(unset)_ | no | Enables GitHub card <-> issue sync (RFC-0019 section 3.5). Unset = sync OFF: no network call on a card write, no issue opened. The bare `GITHUB_TOKEN`/`GH_TOKEN` are deliberately **not** accepted — this credential opens issues in a real repo, so an ambient `gh` login must not switch the feature on. Server-side only. |
| `CFACTORY_GITHUB_REPO` | _(unset)_ | **DEPRECATED — a one-release seed** | `owner/repo` a `ready` card opens its issue in. RFC-0020 §3.3 retired it as configuration on the same seed rule as `CFACTORY_INTAKE_PROJECT_ID`: it is now the tenant's `project`, editable in Settings > Git integration. Unset and unconfigured = cards can only *adopt* an existing issue (set the card's `issue_ref`), never create one. |
| `CFACTORY_GITHUB_API_URL` | `https://api.github.com` | no | GitHub API base. Override for GitHub Enterprise. Since RFC-0020 §3.3 it SEEDS a GitHub tenant's `base_url`; the stored value is authoritative thereafter. |
| `CFACTORY_GIT_PROVIDER` | `github` | **DEPRECATED — a one-release seed** | Which git host the board syncs cards with (RFC-0020 phase 1): `github`, `gitlab` or `azure_devops`. RFC-0020 §3.3 retired it as configuration on the same seed rule: it is now the tenant's `provider`, editable in Settings > Git integration. GitLab/Azure DevOps are served by the fleet's canonical provider layer, vendored at `apps/backend/runners/github/`. |
| `CFACTORY_GIT_PROVIDER_TOKEN` | _(unset)_ | no | Deployment-wide credential for the selected provider — the provider-neutral name for `CFACTORY_GITHUB_TOKEN`; set either, this one wins. Since RFC-0020 §3.4 it is the **fallback**, used only by a tenant that has stored no credential of its own (see `CFACTORY_CREDENTIAL_KEY`), which keeps every existing single-tenant deploy working untouched. Tenants sharing it are not isolated from each other's credential — storing a per-tenant one is what makes them so. Carries the same deliberate omission: the bare `GITHUB_TOKEN`/`GH_TOKEN` are **not** accepted. Server-side only. |
| `CFACTORY_GIT_PROVIDER_URL` | _(unset)_ | **DEPRECATED — a one-release seed** | Base URL of the provider instance (self-hosted GitLab, Azure DevOps server, GitHub Enterprise). RFC-0020 §3.3 retired it as configuration on the same seed rule: it is now the tenant's `base_url`. Unset and unconfigured = the provider's public default (`CFACTORY_GITHUB_API_URL` for GitHub, `https://gitlab.com`, `https://dev.azure.com`). |
| `CFACTORY_IMPORT_STATE` | `open` | no | Which issues a *backfill* imports (RFC-0020 section 3.6): `open`, `closed` or `all`. Deliberately the wide default — "connect my repo" means "show me my backlog", and a filter that quietly hides most of it fails with no error to diagnose. The incremental pass always uses `all` regardless, so closures and reopenings are not missed. |
| `CFACTORY_IMPORT_LABELS` | _(unset)_ | no | Comma-separated label filter for the backfill. Empty = no filter. Opt-in narrowing, never the default. |
| `CFACTORY_IMPORT_MAX` | `1000` | no | Ceiling on issues brought in by one import. Truncation is **reported** in the result and in the board's import summary, never silent. |
| `CFACTORY_IMPORT_POLL` | `true` | no | The background reconciliation loop (#374): re-import each connected repository on a cadence, so issues filed, closed or edited after the first import appear on their own. **On** by default, unlike the other background loops here — a board that silently drifts from the repository is worse than no import, because it looks current. Inert with no configured repository or credential (it resolves nothing and calls nobody). Import is **poll-based, not live**: there is no webhook receiver, so an issue appears within one cadence, never instantly. Set `false` for a deployment that imports only on command, and expect to explain to its users why the board is behind. |
| `CFACTORY_IMPORT_POLL_SECONDS` | `300` | no | Poll cadence, per repository. Five minutes: fast enough that a board is rarely more than a coffee out of date, slow enough that a hundred tenants do not exhaust a rate limit. Also the basis of two derived values: the poll lease is half of it (so two replicas do not both read one repository) and the cockpit calls a repository stale after two of it without a successful read. Very low values are self-defeating — 40 repositories at 5s is ~29 000 requests/hour against GitHub's 5000, which throttles and then backs off. |
| `CFACTORY_IMPORT_POLL_GAP_SECONDS` | `2` | no | Pause between two repositories inside one poll cycle — the rate-limit guard (#374). Forty repositories spread over eighty seconds instead of arriving at the host in one tick. `0` disables the pacing, which is fine for one or two repositories and a burst at forty. |
| `CFACTORY_IMPORT_COMMENTS` | `true` | no | Import an issue's **comments** with its body (Factory#375), refreshed on the same incremental pass. For planning the thread is usually where the decision lives, so a card that drops it drops the decision. Affordable because the refresh uses the provider's bulk path where one exists — on GitHub the repository-wide comments endpoint answers a whole board in **one** request with a server-side `since`, so a 55-card cycle costs two API calls in total (issues + comments). GitLab and Azure DevOps have no such endpoint, so a refresh there is one call per card; set `false` if that is not a trade you want. A failed read stores nothing and marks nothing complete — an issue with no discussion and an issue whose discussion failed to download stay distinguishable via `comments_synced_at`. |
| `CFACTORY_IMPORT_COMMENT_BACKFILL_MAX` | `25` | no | How many **never-read** cards one pass may backfill (Factory#375). Bounds the only unbounded path: a cold backfill has no `since` window to narrow, so it costs one call per card, and a freshly connected 200-issue repository would otherwise fire 200 requests in one tick — the same stampede `CFACTORY_IMPORT_POLL_GAP_SECONDS` prevents one level up. Cards are taken oldest first, so a board becomes comment-complete over consecutive passes; already-read cards still refresh every pass. `0` disables the backfill, which also disables the refresh (only complete copies are refreshed). |
| `CFACTORY_CREDENTIAL_KEY` | _(unset)_ | to store any per-tenant credential | Key-encryption key for the per-tenant git credential store (RFC-0020 §3.4). Format `<key-id>:<base64 of 32 random bytes>`, comma-separated when rotating with the **active key first**. See the full write-up directly below this table. Server-side only. |
| `CFACTORY_AUDIT_HMAC_SECRET` | dev secret (`dev-insecure-...`) | in hosted | HMAC secret anchoring the tamper-evident audit chain. MUST be overridden in any hosted/shared deploy (API keys or multi-tenant set) — the default is a clearly-labelled dev value and startup hard-warns if it is left in place in a non-local posture. Server-side only. |
| `CFACTORY_INSTALL_CALLBACK_BASE_URL` | _(unset)_ | to use the install flow | Public base URL the git provider redirects back to after a human consents (RFC-0020 §3.4 phase 4). It must reach the backend **without passing oauth2-proxy** — a provider redirect arrives unauthenticated and would be bounced to a login page, losing the code — so the deployed value is the MCP host, `https://cfactory-mcp.freundcloud.org.uk`, which already bypasses the auth perimeter. No path, no trailing slash; the callback is always `/git/install/callback`. Unset = the install flow is OFF and the panel keeps the paste box. |
| `CFACTORY_GITHUB_APP_ID` | _(unset)_ | for the GitHub install flow | The GitHub App's numeric App ID, from its settings page. Not a secret. |
| `CFACTORY_GITHUB_APP_SLUG` | _(unset)_ | for the GitHub install flow | The App's URL slug, used to build `https://github.com/apps/<slug>/installations/new`. Read it off the App's public URL rather than deriving it from the name. Not a secret. |
| `CFACTORY_GITHUB_APP_PRIVATE_KEY` | _(unset)_ | for the GitHub install flow | The App's RSA private key, PEM as downloaded from GitHub. **The one deployment-wide secret of the GitHub half** — it signs a short-lived App JWT which mints installation tokens scoped to the repositories the installer selected. Never written to the database and never returned by any API. Multi-line values are fine in an env var. Server-side only. |
| `CFACTORY_GITHUB_APP_PRIVATE_KEY_FILE` | _(unset)_ | alternative to the above | Path to that PEM on disk — **preferred** where the platform mounts secrets as files. Read at use time and not cached, so rotating the mounted key needs no restart, and it **wins** over the inline value so a mounted secret cannot be shadowed by a stale env var. |
| `CFACTORY_GITLAB_OAUTH_CLIENT_ID` | _(unset)_ | for the GitLab install flow | The GitLab OAuth application's Application ID. Not a secret. |
| `CFACTORY_GITLAB_OAUTH_CLIENT_SECRET` | _(unset)_ | for the GitLab install flow | Its Secret — the deployment-wide secret of the GitLab half, used only on back-channel calls to `/oauth/token`. Never in a URL a browser follows, never stored, never returned. Server-side only. |
| `CFACTORY_GITLAB_OAUTH_SCOPE` | `api` | no | Scope requested from GitLab. `api` is what reading and writing issues needs; narrowing it further breaks the board's writes. |

> **Registering the apps is a human step** — GitHub shows the App ID and private
> key to the registrant and to nobody else, and there is no API for it. That is
> exactly why these are deployment configuration: a self-hosted operator registers
> their own rather than depending on somebody else's credentials. The full runbook
> — permissions to select, events to leave unticked, the callback URL to enter,
> and what to do with the private key — is
> [Registering the GitHub App and the GitLab OAuth application](../guides/git-app-install.md).
>
> **Azure DevOps has no install flow** (RFC-0020 §3.4, deliberately). It keeps the
> pasted-credential path above, and no install button is offered for it.

## `CFACTORY_CREDENTIAL_KEY`, in full

> **As an operator**, I want per-tenant git credentials encrypted with a key I
> control and can rotate, **so that** a database dump is not a list of everyone's
> tokens and "we have rotated" is something I can actually do.

RFC-0020 §3.4. Each tenant's git credential is sealed with its own random
256-bit data key (AES-256-GCM); that data key is sealed with **this** value, and
the row records which key version did the wrapping. This is the only thing
standing between the `tenant_git_credential` table and plaintext credentials, so
it belongs in the deployment's secret store (agenix -> the `factory-secrets`
Kubernetes secret), never in a values file or a commit.

**Format.**

```
CFACTORY_CREDENTIAL_KEY=v1:<base64 of 32 random bytes>
```

Bare base64 with no `<key-id>:` prefix is read as key id `v1`, so a single-key
deployment need not invent one. During a rotation, list several — **active key
first**, every older key still present:

```
CFACTORY_CREDENTIAL_KEY=v2:<new base64>,v1:<old base64>
```

**How to generate one.**

```
python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
```

or, equivalently:

```
openssl rand -base64 32
```

It must decode to exactly 32 bytes. A passphrase, a truncated value or bad base64
is **refused at load** with an error saying so — it is never hashed or stretched
into something key-shaped, because that would turn a typo into a weaker key
nobody notices.

**Rotating.** Put the new key first and keep the old one listed. Each stored
credential is re-wrapped onto the new key the next time it is used, which
re-encrypts only the data key and never decrypts the credential. Watch the
`credential.key_version` shown in Settings > Git integration: once every tenant
reports the new id, the old key can be dropped from the variable. A tenant whose
credential is never used never migrates, so check before you drop.

**What happens when it is unset.** Storing a credential is **refused** with a 503
naming the variable, and nothing is written — there is no plaintext fallback and
no derived-from-nothing key. Existing deployments are unaffected: tenants keep
using `CFACTORY_GIT_PROVIDER_TOKEN`, exactly as before.

**What happens if it is lost.** Every stored credential becomes permanently
undecryptable. There is no recovery, no escrow and no backdoor — that is what
authenticated encryption means. The board does not break: affected tenants report
`credential_missing`, every read keeps serving, and each failed decryption is
recorded in the audit chain. The fix is to store the credentials again. Back this
value up wherever you back up `CFACTORY_AUDIT_HMAC_SECRET`.

## Copilot external secret

Read by the Claude Agent SDK — not by CFactory code — when
`CFACTORY_COPILOT_PROVIDER=claude`:

| Variable | Default | Required? | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | _(unset)_ | if provider is claude | Claude Agent SDK credential for the copilot. Server-side only. |

## Frontend (`VITE_*`, build-time)

`VITE_*` variables are read at **build time** by Vite and inlined into the
static bundle (`import.meta.env`) — they are not runtime container env. Set them
as build args / build-environment when running `npm run build` (or in the
frontend image build). Each has a hard-coded fallback, so all are optional.

| Variable | Default (fallback) | Purpose |
|---|---|---|
| `VITE_OBSERVE_URL` | `https://observe.freundcloud.org.uk` (set empty to hide) | OpenObserve dashboard URL linked from the Services view / header. Set to an empty string at build time to hide the link entirely. |
| `VITE_PFACTORY_URL` | `https://pfactory.freundcloud.org.uk` | PFactory (Plan) portal link target in the portal switcher / stage rail. |
| `VITE_AIFACTORY_URL` | `https://aifactory.freundcloud.org.uk` | AIFactory (Build) portal link target. |
| `VITE_TFACTORY_URL` | `https://tfactory.freundcloud.org.uk` | TFactory (Test) portal link target. |

## Frontend container runtime (nginx)

The cockpit is served by nginx, whose config is templated with `envsubst` at
container start. These are container **runtime** env — not `VITE_*`, not backend
`Settings` — documented here for completeness.

| Variable | Default | Required? | Purpose |
|---|---|---|---|
| `BACKEND_URL` | `http://cfactory:80` | yes (in-cluster) | Upstream the nginx `/api` + `/health` + `/api/ws` proxy points at. |
| `CFACTORY_API_KEY` | _(empty)_ | when API keys are enforced | Bare CFactory key nginx injects as `Authorization: Bearer <key>` on `/api` + `/connect`, so the browser cockpit authenticates once the backend keystore is enforced. No `:scopes` suffix. |

## Secrets checklist

Set these to real values in any hosted/shared deployment — never commit them:

- `CFACTORY_AUDIT_HMAC_SECRET` — overrides the clearly-labelled dev default; startup hard-warns if the default is left in a non-local posture.
- `CFACTORY_CREDENTIAL_KEY` — encrypts every per-tenant git credential. Without it, no credential can be stored; **lose it and the stored credentials are unrecoverable**. Back it up alongside the audit secret.
- `CFACTORY_UPSTREAM_TOKEN` / `CFACTORY_AIFACTORY_TOKEN` — upstream factory auth.
- `CFACTORY_API_KEYS` — scoped keys that gate the cockpit API.
- `CFACTORY_MCP_SECRET` — legacy full-scope credential for the MCP transport (`CFACTORY_API_KEYS` also gates `/mcp`, per declared tool scope).
- `CFACTORY_INTAKE_PROJECT_ID` — not a secret, and **no longer where this is set**: since RFC-0020 §3.3 it seeds the tenant's git config once, and the AIFactory project autonomous card-driven builds land in is edited in Settings > Git integration. Still the one value a hosted deploy must decide deliberately.
- `CFACTORY_OLLAMA_API_KEY` / `ANTHROPIC_API_KEY` — copilot credentials.

## Notes

- The three upstream endpoints (`AIFACTORY`/`PFACTORY`/`TFACTORY_API_URL`) and
  the copilot provider + model are also editable at runtime from the cockpit
  (Services and Settings views). Runtime edits persist to small JSON files under
  the workspace root and survive a restart; the copilot API key is never written
  to disk.
- The planning board reads `CFACTORY_API_KEYS` / `CFACTORY_MCP_SECRET` (board
  tool scopes) and `CFACTORY_MULTI_TENANT` (card partitioning). Each is written
  up with its user story, options and failure behaviour in
  [the planning-board guide](../guides/planning-board.md).
- **Which git host and project the board syncs with is no longer an environment
  variable.** RFC-0020 §3.3 made it a tenant resource, edited in Settings > Git
  integration and reachable at `/api/tenants/{tenant}/git-config` (with MCP
  twins). The four variables marked DEPRECATED above seed it once on first boot
  and are removed next release. Since RFC-0020 §3.4 the **credential** is a
  tenant resource too (`/api/tenants/{tenant}/git-credential`, write-only,
  encrypted with `CFACTORY_CREDENTIAL_KEY`); `CFACTORY_GIT_PROVIDER_TOKEN`
  remains the fallback for any tenant that has stored none.
- Deep operator detail — per-variable read location, Helm knobs and chart gaps —
  lives in the in-repo TechDocs at `techdocs/dependencies.md`.

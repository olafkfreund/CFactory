---
layout: default
title: "Registering the GitHub App and the GitLab OAuth application"
permalink: /guides/git-app-install/
---

# Registering the GitHub App and the GitLab OAuth application

> RFC-0020 §3.4 phase 4 (Factory#365). The runbook for the part of the install
> flow **only a human can do.**

CFactory can authenticate a git connection two ways. Phase 3 gave it the first:
paste a personal access token, which is encrypted at rest and works everywhere.
This guide sets up the second, which is better and which nobody but you can turn
on:

- **GitHub** — a GitHub App. It mints short-lived (about one hour) installation
  tokens scoped to the repositories the installer selected, and acts as its own
  identity rather than impersonating a person. The audit trail on your
  repositories says *the app*, and the integration does not break when whoever
  set it up leaves.
- **GitLab** — an OAuth application. No token is pasted; only a refresh
  credential is stored, encrypted, and access tokens are obtained from it on
  demand.
- **Azure DevOps** — deliberately **not** covered. It keeps the phase-3 pasted
  credential, and no install button is shown for it.

**Why you have to do this yourself.** Registering an App produces an App ID and
an RSA private key that GitHub shows to the registrant and to nobody else. There
is no API for it, and there is no shared Factory-owned App to piggyback on — by
design, because a self-hosted operator should not depend on somebody else's
credentials. So the App credentials are **deployment configuration**: you
register, you supply them as environment/secret, and the flow becomes available
to every tenant on your deployment.

---

## 0. Decide the callback host first

The provider redirects a browser back to CFactory when the human has consented.
That redirect arrives **unauthenticated** — a browser navigation carries no
session and no API key — so it must reach a host that does not sit behind
oauth2-proxy, or the redirect is bounced to a login page and the `code` is lost.

**The decision for this deployment: host the callback on
`https://cfactory-mcp.freundcloud.org.uk`.** That host already bypasses
oauth2-proxy for `/mcp`, so nothing about the auth perimeter changes. The
alternative — exempting a path on `https://cfactory.freundcloud.org.uk` — would
cut an unauthenticated hole in the perimeter that fronts the whole cockpit, and
even scoped to one path that is a change to the thing protecting everything
behind it. See `apps/backend/cfactory/routes_install.py` for what the callback
verifies and what it does not.

The callback path is fixed:

```
https://cfactory-mcp.freundcloud.org.uk/git/install/callback
```

Confirm that host actually reaches the backend before you register anything — a
callback URL registered with GitHub that 404s produces a very confusing failure
half an hour later:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  'https://cfactory-mcp.freundcloud.org.uk/git/install/callback?state=probe'
# expect 400 (the state is not one CFactory issued) — NOT 302, 404 or 502.
# A 302 to a login page means the host is behind oauth2-proxy: pick another host.
```

---

## 1. Register the GitHub App

Go to **https://github.com/settings/apps** → *New GitHub App*. (For an
organisation, use `https://github.com/organizations/<org>/settings/apps` instead,
so the App belongs to the org rather than to you personally — recommended.)

Fill in:

| Field | Value |
|---|---|
| **GitHub App name** | e.g. `CFactory Board` (must be globally unique; the slug is derived from it) |
| **Homepage URL** | `https://cfactory.freundcloud.org.uk` |
| **Callback URL** | *(leave empty — this App does not use user-authorization)* |
| **Setup URL (optional)** | `https://cfactory-mcp.freundcloud.org.uk/git/install/callback` |
| **Redirect on update** | **CHECK IT** — so re-selecting repositories comes back through the callback too |
| **Webhook → Active** | **LEAVE IT OFF** — CFactory has no webhook receiver; it polls (RFC-0020 §3.6). Leave it off rather than pointing it at an endpoint that does not exist |
| **Where can this App be installed?** | *Only on this account* unless you intend to offer it to other orgs |

### Repository permissions

Grant exactly these, and nothing else:

| Permission | Level | Why |
|---|---|---|
| **Issues** | **Read & write** | The board opens issues for cards, adopts existing ones, mirrors state back, and imports a repository's backlog |
| **Metadata** | **Read-only** | Mandatory (GitHub selects it automatically); it is also what the connection `Verify` button reads |

Leave **Contents**, **Pull requests**, **Actions**, **Administration** and every
other permission at *No access*. CFactory's board does not read your code, open
pull requests or run anything. If a later phase needs more, it will say so — do
not grant it pre-emptively.

### Organization / account permissions

None. Leave them all at *No access*.

### Subscribe to events

**None.** Every checkbox unticked. There is no webhook receiver to deliver them
to, and a subscription with nowhere to go is noise in your org's audit log.

Click **Create GitHub App**.

### Collect the three values

On the App's settings page, immediately after creation:

1. **App ID** — shown near the top (a number, e.g. `1234567`). Not a secret.
2. **Slug** — the last path segment of the App's public URL
   (`https://github.com/apps/`**`cfactory-board`**). Not a secret. It is the
   name lower-cased and hyphenated, but read it off the URL rather than guessing.
3. **Private key** — scroll to *Private keys* → **Generate a private key**. A
   `.pem` file downloads. **This is the one deployment-wide secret of the whole
   GitHub half, and GitHub will never show it to you again.**

Treat that `.pem` like a root key:

```bash
# Move it somewhere it will not be committed, and lock it down.
mkdir -p ~/.secrets && chmod 700 ~/.secrets
mv ~/Downloads/cfactory-board.*.private-key.pem ~/.secrets/cfactory-github-app.pem
chmod 600 ~/.secrets/cfactory-github-app.pem
```

Never paste it into a chat, a ticket, a repository or a CFactory form. CFactory
has no field that accepts it — it is configuration, not tenant data.

> **If it leaks:** revoke it on the App's settings page (*Private keys* →
> delete), generate a new one, and update the secret. Existing installations
> survive; only the signing key changes.

---

## 2. Register the GitLab OAuth application

Skip this if you do not use GitLab.

On gitlab.com or your self-hosted instance, go to the level you want the app to
belong to:

- personal: **User settings → Applications**
- group-wide (recommended): **Group → Settings → Applications**
- instance-wide (self-hosted, admin): **Admin → Applications**

Fill in:

| Field | Value |
|---|---|
| **Name** | `CFactory Board` |
| **Redirect URI** | `https://cfactory-mcp.freundcloud.org.uk/git/install/callback` |
| **Confidential** | **CHECK IT** — the secret is held server-side and never reaches a browser |
| **Scopes** | **`api`** only |

`api` is what reading and writing issues needs; nothing narrower covers it. Do
**not** tick `read_repository`, `write_repository`, `sudo` or `admin_mode`.

Save, then collect:

1. **Application ID** — not a secret.
2. **Secret** — shown once. This is the deployment-wide secret of the GitLab
   half. Store it the same way as the PEM.

> **Group access tokens are the alternative** the RFC also allows. If you would
> rather use one, do not register anything here — create the group access token
> with the `api` scope and paste it into the connection's **Credential** box
> instead. That is the phase-3 path and it still works; it just has no refresh
> and expires on the date you chose.

---

## 3. Supply the credentials to the deployment

All of these are `CFACTORY_`-prefixed environment variables read by
`apps/backend/cfactory/config.py`. None is ever written to the database and none
is returned by any API.

| Variable | Secret? | Value |
|---|---|---|
| `CFACTORY_INSTALL_CALLBACK_BASE_URL` | no | `https://cfactory-mcp.freundcloud.org.uk` (no trailing slash, no path) |
| `CFACTORY_GITHUB_APP_ID` | no | the App ID from step 1 |
| `CFACTORY_GITHUB_APP_SLUG` | no | the slug from step 1 |
| `CFACTORY_GITHUB_APP_PRIVATE_KEY_FILE` | — | path to the mounted `.pem` (**preferred**) |
| `CFACTORY_GITHUB_APP_PRIVATE_KEY` | **YES** | the PEM inline, if you cannot mount a file |
| `CFACTORY_GITLAB_OAUTH_CLIENT_ID` | no | the Application ID from step 2 |
| `CFACTORY_GITLAB_OAUTH_CLIENT_SECRET` | **YES** | the Secret from step 2 |

`_FILE` wins over the inline value when both are set, so a mounted secret can
never be shadowed by a stale environment variable. The file is read at use time
and not cached, so rotating the key does not need a restart.

You also need `CFACTORY_CREDENTIAL_KEY` from phase 3 — the GitLab refresh
credential is sealed with it. GitHub needs no credential key at all, because
GitHub stores no secret.

### Kubernetes

Create the Secret out of band (it holds the PEM, so never in git):

```bash
kubectl -n factory create secret generic cfactory-git-app \
  --from-file=github-app-private-key.pem=$HOME/.secrets/cfactory-github-app.pem \
  --from-literal=gitlab-oauth-client-secret='<the GitLab secret>'
```

Then wire it through the chart's existing `config.extraEnv` escape hatch — the
same route `CFACTORY_CREDENTIAL_KEY` already takes:

```yaml
# values.yaml
config:
  extraEnv:
    - name: CFACTORY_INSTALL_CALLBACK_BASE_URL
      value: https://cfactory-mcp.freundcloud.org.uk
    - name: CFACTORY_GITHUB_APP_ID
      value: "1234567"
    - name: CFACTORY_GITHUB_APP_SLUG
      value: cfactory-board
    - name: CFACTORY_GITHUB_APP_PRIVATE_KEY
      valueFrom:
        secretKeyRef:
          name: cfactory-git-app
          key: github-app-private-key.pem
    - name: CFACTORY_GITLAB_OAUTH_CLIENT_ID
      value: "abc123..."
    - name: CFACTORY_GITLAB_OAUTH_CLIENT_SECRET
      valueFrom:
        secretKeyRef:
          name: cfactory-git-app
          key: gitlab-oauth-client-secret
```

(A multi-line PEM is fine in an env var. If you would rather mount it as a file,
add a volume and set `CFACTORY_GITHUB_APP_PRIVATE_KEY_FILE` to the mount path
instead.)

### Local development

```bash
export CFACTORY_INSTALL_CALLBACK_BASE_URL=http://localhost:3111
export CFACTORY_GITHUB_APP_ID=1234567
export CFACTORY_GITHUB_APP_SLUG=cfactory-board-dev
export CFACTORY_GITHUB_APP_PRIVATE_KEY_FILE=$HOME/.secrets/cfactory-github-app.pem
```

Register a **second, separate** App for local development with
`http://localhost:3111/git/install/callback` as its Setup URL. Do not point your
production App at localhost.

---

## 4. Verify it worked

Restart the backend, then:

1. Open **Settings → Git connections** in the cockpit.
2. A GitHub or GitLab connection now shows a **Connect GitHub** / **Connect
   GitLab** button above the credential box. If it does not, the deployment does
   not think an app is registered — check all of
   `CFACTORY_INSTALL_CALLBACK_BASE_URL`, `_APP_ID`, `_APP_SLUG` and the private
   key are set. `GET /api/tenants/{tenant}/git-connections` reports
   `install_available` and is the quickest way to see which one is missing.
3. Click it. You land on GitHub's install screen. **Choose the repositories** —
   `Only select repositories` is the point of the whole exercise; do not grant
   *All repositories* out of habit.
4. Approve. You come back to a plain "Connected" page on the MCP host. Switch
   back to your Settings tab and reload.
5. The connection shows **Installed on `<your org>`**. Press **Verify** — it
   should go green, having minted a fresh installation token to do it.

Nothing about your token appears anywhere in the panel, and nothing was stored:
`GET /api/tenants/{tenant}/git-connections` reports the `installation_id` and the
account, which are identifiers, and no credential.

---

## 5. Operating it

**Changing which repositories the App can see.** Do it on GitHub (*Settings →
Applications → Configure*), or press **Reconnect** in the panel, which sends you
to the same screen. CFactory picks up the new scope on the next mint; no
reconfiguration here.

**When a refresh fails.** The connection drops to `credential_missing` and the
panel shows the provider's reason under the connected app. Board writes then
**fail loudly** — the card carries the error and the sync reports `ok: false` —
rather than silently doing nothing. The usual cause is the App being uninstalled
or the GitLab grant being revoked at the provider; press **Reconnect**.

**Disconnecting.** **Disconnect** makes CFactory forget the installation and any
stored refresh credential. It does **not** remove the app's access to your
repositories — only the account owner can do that, on GitHub's *Installed GitHub
Apps* page or GitLab's *Authorized applications*. Do both if you are revoking for
real.

**Rotating the private key.** Generate a new one on the App's settings page,
update the secret, delete the old key. Installations are unaffected; the next
mint uses the new key. No CFactory state changes.

**Losing the private key** means no installation token can be minted for any
tenant: every installed connection degrades to `credential_missing` at once, with
a visible reason. Generate a new key and update the secret — the
`installation_id`s are still there and nothing needs reinstalling.

---

## What CFactory actually stores

Worth being precise, because it is the argument for doing all of the above:

| Value | Where it lives | Lifetime |
|---|---|---|
| GitHub App private key | your deployment's environment/secret. **Never** the database | until you rotate it |
| GitHub `installation_id` | `git_install` table, plaintext — it is an identifier, not a secret | until disconnected |
| GitHub installation token | **process memory only**, never written | ~1 hour, minted on demand |
| GitLab client secret | your deployment's environment/secret | until you rotate it |
| GitLab refresh credential | `tenant_git_credential`, AES-256-GCM sealed against (tenant, connection) — the phase-3 store | until disconnected or rotated by GitLab |
| GitLab access token | **process memory only**, never written | ~2 hours, refreshed on demand |

A minted token reaching the database is prevented by one function
(`git_install.persistable_secret`) and pinned by a mutation-checked test
(`tests/test_git_install.py::test_a_minted_installation_token_is_never_persisted`).

## Related

- [Multi-tenant deployment](./multi-tenant.md)
- [GitHub card sync](./github-card-sync.md)
- [Environment reference](../dev/environment-reference.md)

---
status: approved
issue: 530
intent: intent/2026-11-17-530-cfactory-claude-creds.md
---

# Spec: cfactory Claude credentials via cred-sync

## Design

Copy the pfactory wiring into `apps/cfactory/manifests/manifests.yaml` (factory-gitops), trimmed to Claude only:

- initContainer `seed-creds` (busybox, same digest): copies `/seed/claude-credentials.json` to `/h/.claude/.credentials.json` so the first request has a login.
- sidecar `cred-sync` (python:3.12-slim, same digest, script and preStop byte-identical to the other three); mounts `cc-claude` and `cli-creds`.
- app container: mount `cc-claude` at `/home/nonroot/.claude`, env `CLAUDE_CONFIG_DIR=/home/nonroot/.claude` (the CLI writes `~/.claude.json` otherwise, which fails on the read-only root fs).
- volumes: `cc-claude` emptyDir, `cli-creds` secret `factory-cli-creds` (already exists, shared).
- `.github/scripts/test_cred_sync.py`: add `cfactory` to `APPS` so the drift and behaviour guards cover it.
- CFactory `RELEASE.md`: note that copilot verification depends on cred-sync and which log line shows it.

## Alternatives rejected

- Skip the SDK query in release verification: rejected, the copilot genuinely needs Claude and would stay broken for users.
- `CLAUDE_CODE_OAUTH_TOKEN` env from factory-secrets: needs a secret the owner must mint; the lineage-refresh pattern already exists.
- Switch the copilot default to ollama: changes product behaviour, not asked.

## Risks

- cfactory pod restarts on sync (Recreate strategy): brief copilot/cockpit-backend gap.
- Memory limit 1Gi: the SDK spawns the bundled claude CLI; if it OOMs, raise the limit later (not guessed here).
- The bundled CLI may want other writable dirs; `/tmp` and `.claude` are writable, `.cache` is not mounted (add if observed).

## Verification

`python3 .github/scripts/test_cred_sync.py`; kustomize/yaml parse of the manifest; after sync (owner/coordinator): `kubectl logs deploy/cfactory -c cred-sync` and a copilot question.

Codex review note (unverified, accepted): read-only-root runtime compatibility of the bundled CLI cannot be proven without live credentials; it is checked after sync via the copilot question, and `.cache` is added only if observed.

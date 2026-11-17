---
status: approved
issue: 530
spec: spec/2026-11-17-530-cfactory-claude-creds.md
coders:
  1: you (lead; small YAML copy, no handoff)
  2: you
  3: you
reviewers:
  - codex   # independent read-only review of the final diff
---

# Plan: cfactory Claude credentials via cred-sync

Decisions carried from the approved spec: wire Claude credentials into the cfactory pod by copying pfactory's cred-sync pattern (shared `factory-cli-creds`, no new secrets); do not skip the SDK query. Steps 1-2 are in factory-gitops (branch feat/530-cfactory-cred-sync, base main); step 3 is in CFactory (base dev).

## Steps

1. factory-gitops `apps/cfactory/manifests/manifests.yaml`, cfactory Deployment: add initContainer `seed-creds` (Claude file only), sidecar `cred-sync` (byte-copy of pfactory's container block incl. lifecycle/resources), app mount `cc-claude` -> `/home/nonroot/.claude`, env `CLAUDE_CONFIG_DIR`, volumes `cc-claude` emptyDir and `cli-creds` secret `factory-cli-creds`. -> verify by `python3 -c "import yaml;list(yaml.safe_load_all(open("apps/cfactory/manifests/manifests.yaml")))"`.
   Traps: cred-sync text must be byte-identical; keep readOnlyRootFilesystem; do not touch the frontend Deployment.
2. factory-gitops `.github/scripts/test_cred_sync.py`: add `cfactory` to `APPS`, reword "three" messages to "vendored". -> verify by `python3 .github/scripts/test_cred_sync.py` (needs unshare; if unavailable the drift part still runs first).
   Traps: extract() needs exactly one `- name: cred-sync` per manifest.
3. CFactory `RELEASE.md`: add "Post-deploy: copilot (Claude) check" note. -> verify by reading; docs-only.

## Tests

`python3 .github/scripts/test_cred_sync.py` passes in factory-gitops; its CI (`cred-sync guards`) green.

## Rollback

Revert the factory-gitops PR; Argo restores the previous Deployment. The Secret is untouched.

## Live acceptance (owner/coordinator, after Argo sync)

`kubectl logs deploy/cfactory -c cred-sync` shows no refusal, and a copilot question with provider `claude` returns an answer. Not provable here without live credentials.

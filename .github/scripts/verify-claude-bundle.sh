#!/usr/bin/env bash
# Verify the SDK and bundled claude binary in a built backend image.
# Usage: verify-claude-bundle.sh <image> [lockfile]
set -euo pipefail

image="${1:?usage: verify-claude-bundle.sh <image> [lockfile]}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
lockfile="${2:-${repo_root}/apps/backend/claude-bundle.lock}"

if [[ ! -f "${lockfile}" ]]; then
  echo "::error::Missing claude-bundle.lock: ${lockfile}" >&2
  exit 1
fi

version=''
sha256=''
platform=''
while IFS= read -r line || [[ -n "${line}" ]]; do
  case "${line}" in
    ''|'#'*) continue ;;
    *=*) ;;
    *) echo "::error::Malformed claude-bundle.lock line: ${line}" >&2; exit 1 ;;
  esac
  key="${line%%=*}"
  value="${line#*=}"
  case "${key}" in
    version) version="${value}" ;;
    sha256) sha256="${value}" ;;
    platform) platform="${value}" ;;
    *) echo "::error::Unknown claude-bundle.lock key: ${key}" >&2; exit 1 ;;
  esac
done < "${lockfile}"

if [[ -z "${version}" || -z "${sha256}" || -z "${platform}" ]]; then
  echo "::error::claude-bundle.lock requires version, sha256, and platform" >&2
  exit 1
fi
if [[ ! "${sha256}" =~ ^[[:xdigit:]]{64}$ || "${platform}" != linux/amd64 ]]; then
  echo "::error::claude-bundle.lock requires a 64-digit hex sha256 and platform=linux/amd64" >&2
  exit 1
fi

if ! architecture="$(docker image inspect -f '{{.Architecture}}' "${image}")"; then
  echo "::error::Could not inspect image architecture: ${image}" >&2
  exit 1
fi
if [[ "${architecture}" != amd64 ]]; then
  echo "::error::Image architecture is ${architecture}; claude-bundle.lock requires ${platform}" >&2
  exit 1
fi

python_code='import hashlib, importlib.metadata, pathlib, claude_agent_sdk; print(importlib.metadata.version("claude-agent-sdk")); print(hashlib.file_digest(open(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude", "rb"), "sha256").hexdigest())'
if ! actual="$(docker run --rm --entrypoint python "${image}" -c "${python_code}")"; then
  echo "::error::Could not verify bundled claude binary in ${image}" >&2
  exit 1
fi
mapfile -t lines <<< "${actual}"
if [[ "${#lines[@]}" -ne 2 ]]; then
  echo "::error::Expected version and sha256 from ${image}, got: ${actual}" >&2
  exit 1
fi
if [[ "${lines[0]}" != "${version}" || "${lines[1]}" != "${sha256}" ]]; then
  echo "::error::claude-bundle.lock mismatch: expected version=${version} sha256=${sha256}; actual version=${lines[0]} sha256=${lines[1]}" >&2
  echo "::error::update claude-agent-sdk== in apps/backend/requirements.txt and apps/backend/claude-bundle.lock together" >&2
  exit 1
fi

echo "OK: claude-agent-sdk ${version} sha256=${sha256}"

# CFactory backend image.
#
# FastAPI app `cfactory.app:app` served by uvicorn. The package root is
# `apps/backend`, so PYTHONPATH must point there. The server is pinned to
# `--http h11 --ws wsproto` because httptools does not forward the WebSocket
# Upgrade in this stack (the /api/ws cockpit feed needs it).
#
# Base: slim Python 3.13. FUTURE HARDENING: the sibling Factory family targets
# Chainguard distroless (cgr.dev/chainguard/python) for a non-root, minimal,
# CVE-scanned runtime — migrate here once the build is stable.

FROM python:3.14-slim AS base

# Faster, quieter, deterministic Python + pip behaviour.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/apps/backend

WORKDIR /app

# Apply Debian security updates to the base layer (CFactory#396).
#
# `python:3.14-slim` is a floating tag, so this is NOT a stale-pin problem that
# a digest bump fixes: the tag was already resolving to the newest published
# build and that build still carried util-linux 2.41-5. Debian had already
# shipped the fix — 2.41.5-0+deb13u1 in trixie's security suite, for
# CVE-2026-53615 — across nine binary packages (bsdutils, libblkid1,
# liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux),
# which is precisely the nine fixable HIGHs the Trivy gate was failing on. The
# upstream image simply had not been rebuilt against it yet. Upstream rebuild
# lag is normal and recurring; pulling the fix ourselves is the fix.
#
# `upgrade`, not a hand-listed set of packages: the finding is base-OS lag, so
# the remediation is "do not ship a base layer behind Debian security", not
# "chase whichever package Trivy named this week". On the base as of this
# commit it installs exactly those nine and nothing else (`apt-get -s upgrade`).
#
# No `--no-install-recommends`: `upgrade` never adds packages, only replaces
# installed ones. The apt lists are removed in the same layer so they are not
# baked into the image.
#
# SECURITY_REFRESH busts THIS layer's cache. CI builds with `cache-from:
# type=gha`, so without it the upgrade layer is served from cache and never
# re-runs -- the image keeps shipping whatever Debian shipped the day the
# layer was first built. That is exactly how CVE-2026-14456 (openssl
# 3.5.6 -> 3.5.7) reached a gate failure on an image whose Dockerfile already
# ran `apt-get upgrade`: the upgrade was real, its result was frozen.
# CI passes the current date, so the layer rebuilds at most once a day.
ARG SECURITY_REFRESH=0
RUN echo "security refresh: ${SECURITY_REFRESH}" \
    && apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*

# Install runtime dependencies first so the layer caches across code changes.
#
# Then remove pip itself. This is CVE remediation, not tidying: pip vendors a
# private copy of its own dependencies under `pip/_vendor/`, and ships a
# CycloneDX SBOM (`pip/_vendor/bom.cdx.json`) that Trivy reads. pip 26.2.1 --
# the newest release, and what python:3.14-slim bakes in -- pins
# `msgpack==1.1.2` (GHSA-6v7p-g79w-8964) and `setuptools==70.3.0`
# (CVE-2025-47273) in `_vendor/vendor.txt`. Both are the ONLY source of those
# two findings in this image; neither is a CFactory dependency, direct or
# transitive, so there is nothing in requirements.txt to bump, and upgrading
# pip cannot help because 26.2.1 is already latest.
#
# Deleting pip is the real fix rather than a suppression: the vulnerable code
# leaves the image instead of the scanner being told to look away. It is also
# correct on its own terms -- the runtime entrypoint is `python -m uvicorn`,
# nothing here installs packages at run time, and a production image that
# cannot fetch and execute arbitrary code from PyPI is the stronger one.
#
# `pip uninstall` removes every path in pip's RECORD, `_vendor/` and the SBOM
# with it. Asserted by tests/test_runtime_image_has_no_pip.py.
COPY apps/backend/requirements.txt ./apps/backend/requirements.txt
RUN pip install --requirement apps/backend/requirements.txt \
    && python -m pip uninstall --yes pip

# Copy the backend package (frontend is a separate static build).
COPY apps/backend/ ./apps/backend/

# Run as a non-root user.
RUN useradd --create-home --uid 65532 --user-group nonroot \
    && chown -R nonroot:nonroot /app
USER nonroot

EXPOSE 3111

# h11 + wsproto: httptools does not forward the WS Upgrade in this stack.
CMD ["python", "-m", "uvicorn", "cfactory.app:app", \
     "--host", "0.0.0.0", "--port", "3111", \
     "--http", "h11", "--ws", "wsproto"]

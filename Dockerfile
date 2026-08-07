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

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

FROM python:3.13-slim AS base

# Faster, quieter, deterministic Python + pip behaviour.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/apps/backend

WORKDIR /app

# Install runtime dependencies first so the layer caches across code changes.
COPY apps/backend/requirements.txt ./apps/backend/requirements.txt
RUN pip install --requirement apps/backend/requirements.txt

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

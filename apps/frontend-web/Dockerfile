# CFactory cockpit (frontend) image.
#
# Two-stage: build the Vite bundle with Node, then serve the static dist with
# unprivileged nginx, proxying /api + /health + /api/ws to the backend Service.
# The backend target is configurable at runtime via $BACKEND_URL (envsubst on
# the nginx template at container start).

FROM node:26-alpine AS build
# tsconfig.json extends ../../standards/tsconfig.base.json (Factory#546), so the
# vendored baseline must sit two levels above the app INSIDE the image as well.
# WORKDIR mirrors the repo layout so that relative path means the same thing here
# as it does in git, rather than resolving somewhere by accident. `standards` is a
# NAMED BUILD CONTEXT (build-contexts: in image-build.yml / deploy.yml) because
# the build context proper is apps/frontend-web and the baseline lives outside it.
# Without this the extends silently misses, tsc falls back to its ES5 defaults,
# and the build dies on "Cannot find name 'Map'" a long way from the cause.
WORKDIR /app/apps/frontend-web
COPY --from=standards tsconfig.base.json /app/standards/tsconfig.base.json
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Runtime: nginx-unprivileged listens on 8080 and runs as uid 101 (non-root).
FROM nginxinc/nginx-unprivileged:1.31-alpine AS runtime

# This image had NO security-upgrade layer at all, so it shipped whatever the
# pinned nginx tag was built with -- that is how CVE-2026-14456 (openssl
# 3.5.7-r0 -> 3.5.8-r0) reached a blocking Trivy finding here while the backend
# image, which does upgrade, came back clean.
#
# SECURITY_REFRESH changes daily so the layer is not served from the build
# cache forever; without it the upgrade would run exactly once and freeze.
# Runs as root, then drops back to the image's non-root uid.
USER root
ARG SECURITY_REFRESH=0
RUN echo "security refresh: ${SECURITY_REFRESH}" \
    && apk upgrade --no-cache
USER 101

# Backend Service the cockpit's /api + /health + /api/ws calls are proxied to.
# Overridden by the Helm chart (http://<release>-cfactory:80).
ENV BACKEND_URL=http://cfactory:80

# Token-gated API: the bare CFactory key nginx injects as `Authorization: Bearer
# <key>` on /api + /connect. Defaults to empty so envsubst always resolves the
# placeholder (a harmless "Bearer " the open keystore ignores); the Helm chart
# overrides it from a Secret when the keystore is enforced.
ENV CFACTORY_API_KEY=""

# The official entrypoint renders /etc/nginx/templates/*.template via envsubst
# (only listed env vars are substituted, so nginx's own $vars are preserved).
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/apps/frontend-web/dist /usr/share/nginx/html

EXPOSE 8080

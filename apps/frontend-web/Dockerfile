# CFactory cockpit (frontend) image.
#
# Two-stage: build the Vite bundle with Node, then serve the static dist with
# unprivileged nginx, proxying /api + /health + /api/ws to the backend Service.
# The backend target is configurable at runtime via $BACKEND_URL (envsubst on
# the nginx template at container start).

FROM alpine:3.20 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Runtime: nginx-unprivileged listens on 8080 and runs as uid 101 (non-root).
FROM nginxinc/nginx-unprivileged:1.31-alpine AS runtime

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
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080

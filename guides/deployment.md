# CFactory Deployment

This guide covers building the CFactory backend image, running it locally with
Docker, and deploying it to Kubernetes with the bundled Helm chart.

The backend is a FastAPI app (`cfactory.app:app`). The package root is
`apps/backend`, so the image sets `PYTHONPATH=apps/backend`. The server runs on
port **3111** under uvicorn with `--http h11 --ws wsproto` (httptools does not
forward the WebSocket Upgrade in this stack, which the `/api/ws` cockpit feed
needs). The frontend (`apps/frontend-web`) is a separate static build on 3110
and is not part of this image.

## Build the image

From the repository root:

```bash
docker build -t cfactory:dev .
```

The build installs `apps/backend/requirements.txt`, copies `apps/backend/`, and
runs as a non-root user. The base is `python:3.13-slim`; a future hardening step
is to move to a Chainguard distroless base, matching the rest of the Factory
family.

## Run with Docker

```bash
docker run --rm -p 3111:3111 \
  -e CFACTORY_AIFACTORY_API_URL=http://host.docker.internal:3101 \
  -e CFACTORY_PFACTORY_API_URL=http://host.docker.internal:3102 \
  -e CFACTORY_TFACTORY_API_URL=http://host.docker.internal:3103 \
  cfactory:dev
```

Then check the health endpoint:

```bash
curl -f http://localhost:3111/health
```

Useful environment variables (all prefixed `CFACTORY_`; see
`apps/backend/cfactory/config.py`):

| Variable                      | Purpose                                                    |
| ----------------------------- | ---------------------------------------------------------- |
| `CFACTORY_BACKEND_PORT`       | Listen port (default 3111; matches the image CMD).         |
| `CFACTORY_AIFACTORY_API_URL`  | AIFactory upstream base URL (local default `:3101`).       |
| `CFACTORY_PFACTORY_API_URL`   | PFactory upstream base URL (local default `:3102`).        |
| `CFACTORY_TFACTORY_API_URL`   | TFactory upstream base URL (local default `:3103`).        |
| `CFACTORY_SUBSCRIBE_UPSTREAMS`| Connect to upstream WebSockets on startup (default false). |
| `CFACTORY_DATABASE_URL`       | WorkItem correlation store DSN (optional).                 |
| `CFACTORY_API_KEYS`           | Scoped keys `"<key>:read,write;<key2>:read"` (optional).   |
| `CFACTORY_COPILOT_MODEL`      | Claude Agent SDK model id.                                 |

When `CFACTORY_API_KEYS` is empty the app runs in OPEN (single-user local) mode.
The copilot reads `ANTHROPIC_API_KEY` from the environment.

## Deploy with Helm

The chart lives in `charts/cfactory/`. Lint and preview the rendered manifests:

```bash
helm lint charts/cfactory
helm template cfactory charts/cfactory
```

Install (or upgrade) into a namespace, pointing at your image and the in-cluster
upstream Services:

```bash
helm upgrade --install cfactory charts/cfactory \
  --namespace cfactory --create-namespace \
  --set image.repository=ghcr.io/dataseeek/cfactory \
  --set image.tag=0.1.0 \
  --set config.aifactoryApiUrl=http://aifactory.ai.svc.cluster.local:3101 \
  --set config.pfactoryApiUrl=http://pfactory.pf.svc.cluster.local:3102 \
  --set config.tfactoryApiUrl=http://tfactory.tf.svc.cluster.local:3103
```

### Secrets (production)

Scoped API keys and the database URL are injected from Kubernetes Secrets, not
the ConfigMap. Create them out-of-band, then enable the wiring:

```bash
kubectl -n cfactory create secret generic cfactory-api-keys \
  --from-literal=api-keys='prodkey:read,write;robot:read'

kubectl -n cfactory create secret generic cfactory-db \
  --from-literal=database-url='postgresql://user:pass@db:5432/cfactory'

helm upgrade --install cfactory charts/cfactory --namespace cfactory \
  --set apiKeys.enabled=true \
  --set database.enabled=true
```

### Probes and exposure

Liveness and readiness probes hit `/health`. The Service is `ClusterIP` on port
80 forwarding to container port 3111; front it with your own Ingress/Gateway.

### Scaling note

`replicaCount` is pinned to 1 for v0.x: the cockpit WebSocket fan-out is
single-replica until a shared pub/sub backend lands.

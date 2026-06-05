# Live agents

The **Live agents** panel in Mission Control streams each running AIFactory
agent's terminal directly into the cockpit, read-only. It turns the pipeline's
"Code" stage from a status dot into a window you can watch.

## Data path

```
browser (xterm.js)
   │  WS  /api/live-agents/{correlation_key}/ws        (same origin as the cockpit)
   ▼
CFactory backend  ──── lists active tasks ───►  AIFactory  GET /api/tasks
   │   (proxy, server-side)                     AIFactory  GET /api/capabilities → {"rmux": bool}
   └──── opens rmux console ──────────────────► AIFactory  WS /api/tasks/{spec_id}/agent-console/ws
                                                            (ANSI pane bytes)
```

1. **Discovery** — `GET /api/live-agents` derives the active agent set from the
   AIFactory task list (the same `/api/tasks` the cockpit already polls),
   filtered to non-terminal statuses, and gated on AIFactory's rmux capability.
   Each agent carries a cockpit-side `ws_path`.
2. **Streaming** — for each tile, the browser opens
   `WS /api/live-agents/{correlation_key}/ws` on the **CFactory backend**. The
   backend resolves the correlation key to an AIFactory `spec_id`, opens the
   upstream rmux console WebSocket, and pumps its ANSI bytes down to xterm.js.

### Why a backend proxy (not browser → AIFactory)

The browser never learns the AIFactory URL and never holds an AIFactory token —
both stay server-side in CFactory. The cockpit stays single-origin (no extra
CORS surface), and the backend is the one place to add aggregation or limits.

## Read-only by design

The cockpit **observes**; it never drives an agent. The proxy never calls
AIFactory's `/attach` endpoint and never forwards keystrokes upstream — frames
from the browser are drained solely to detect disconnect. xterm.js runs with
`disableStdin` and no cursor.

## Enabling it

Live agents light up only when AIFactory's rmux console is on.

| Setting | Where | Purpose |
|---|---|---|
| `AIFACTORY_RMUX_ENABLED` / `APP_RMUX_ENABLED` | **AIFactory** | Mounts the agent-console routes and `{"rmux": true}` capability. Required. |
| `CFACTORY_AIFACTORY_API_URL` | CFactory | AIFactory base URL (default `http://localhost:3101`). |
| `CFACTORY_AIFACTORY_TOKEN` | CFactory | Optional. Sent as `Authorization: Bearer <token>` on the upstream rmux WS. Leave unset for local dev where AIFactory runs with `DISABLE_AUTH`. The token stays server-side — it is **never** sent to the browser. |

Steps:

1. Start AIFactory with `APP_RMUX_ENABLED=true` (port `3101`).
2. (Hosted only) set `CFACTORY_AIFACTORY_TOKEN` to a valid AIFactory service token.
3. Start the CFactory backend (`3111`) and frontend (`3110`). The Vite dev server
   proxies `/api` with `ws: true`, so the proxy WebSocket works in development.

## Degraded states

The panel never errors — it degrades:

- **rmux off / AIFactory unreachable** → "Live agents are off — AIFactory's rmux
  console is disabled." (`/api/live-agents` returns `rmux_enabled: false`.)
- **rmux on, nothing running** → "No agents running right now."
- **a stream drops** → the tile prints `— stream ended —` and stops; other tiles
  are unaffected.

## Source

- Backend discovery: `apps/backend/cfactory/live_agents.py`
- Backend proxy: `apps/backend/cfactory/live_agent_proxy.py`
- Frontend: `apps/frontend-web/src/LiveAgents.tsx`

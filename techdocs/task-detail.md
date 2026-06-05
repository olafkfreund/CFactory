# Task detail

On the **Pipeline** board, clicking a work-item card opens a **task detail
drawer** — live information about the task and the process it's in, plus an
embedded read-only rmux terminal for any running task.

## What the drawer shows

- **Per-stage state** — Plan / Code / Test status + phase for the work item.
- **Process** — for the code task: phase, overall and phase progress bars, the
  current subtask, and the subtask checklist. Updates live from the `/api/ws`
  progress feed while the drawer is open.
- **Live terminal** — if the task has an active agent, its rmux console streams
  inline (the same proxy the Live Agents panel uses). Otherwise: "no active
  terminal".
- **Timeline** — the ordered completion-event history (newest first).

## Data path

```
Pipeline card  ──click──►  TaskDetail drawer
   │
   ├─ GET /api/workitems/{key}/process   ─► CFactory backend
   │      └─ proxies AIFactory  GET /api/tasks/{spec_id}
   │         (executionProgress: phase, %, currentSubtask; subtasks[])  → normalized
   ├─ GET /api/live-agents   (is this key streamable?)
   │      └─ WS /api/live-agents/{key}/ws  ─► rmux console (read-only)
   └─ /api/ws feed   (live status + progress updates)
```

`GET /api/workitems/{key}/process` resolves the work item's AIFactory (code)
slice, calls that service's REST detail endpoint, and normalizes it to a stable
shape. Best-effort: `available: false` (with whatever slice state we have) when
there's no task or the service is unreachable, so the drawer degrades cleanly.

## Why REST, not the MCP server

The siblings ship a remote MCP server, but its `get_task` tool simply delegates
to the same `GET /api/tasks/{id}` REST endpoint (and truncates fields). CFactory
already speaks REST to the siblings via its adapters, so reusing that — plus the
existing rmux WS proxy for the terminal — is less integration and matches
[DEC-002](decisions.md) (CFactory consumes REST/WebSocket; MCP is for the
copilot, not the data plane).

## Source

- Backend: `apps/backend/cfactory/task_process.py`, route in `app.py`
- Frontend: `apps/frontend-web/src/TaskDetail.tsx` (reuses `AgentTerminal` from `LiveAgents.tsx`)

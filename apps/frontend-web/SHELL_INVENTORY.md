# CFactory shell action inventory

| Previous control | Shared shell home | Behavior |
| --- | --- | --- |
| Portal switcher | Top bar portal switcher | Opens sibling portals; keeps a resolved project where supported. |
| Project breadcrumb and All projects | Top bar project switcher | Selects a project or clears scope without a page reload. |
| Mission Control, Needs you, Backlog, Planning board, Pipeline, Active tasks, Tokens, Audit, Services | Sidebar navigation and Ctrl+1 through Ctrl+9 | The last three fleet views clear project scope. |
| Settings | Sidebar Fleet settings | Opens the fleet-wide settings view, including from a project URL. |
| Observe | Sidebar external link | Opens Observe in a new tab. |
| Refresh, backend health, live status | Top bar status | Keeps the existing refresh and health behavior. |
| Theme toggle | Top bar theme switch | Remembers Light, Dark, or System; System follows the OS. |
| Search / Ctrl+K | Top bar search and keyboard shortcut | Opens the existing command palette and task search. |
| Pipeline stage and failed badges | Shared strip | Stage and failure filters open Pipeline; clearing the stage clears failures. |
| Copilot launcher | Existing floating launcher | Fleet-wide Copilot remains available. |
| Event ticker | Existing workspace footer | Pins and live events remain available. |
| Log Out placeholder | Removed | The previous button had no handler. |

Planning and Backlog cards keep their native controls inside shared `f-card` styling. Pipeline cards open the existing task detail with its gated actions.

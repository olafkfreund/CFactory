"""CFactory MCP server — the single PARR-pipeline visibility surface.

Implements a minimal POST-only JSON-RPC 2.0 MCP transport at ``POST /mcp`` so an
external agent (Claude Code, the ``/parr-run`` conductor) can watch the whole
plan -> code -> test pipeline from one place. CFactory already correlates every
factory's state by ``correlation_key`` (the GitHub issue number); this exposes
that aggregation read-only as MCP tools instead of forcing the agent to poll
three separate factories.

Read-only for the PARR pipeline view; the RFC-0019 Phase 2b **board tools** add
the write half, so an agent manages the planning backlog exactly as a human does
in the cockpit (§3.3, programmatic equivalence). Those tools do not reimplement
anything: they call :mod:`cfactory.card_ops`, the same store + audit + intake
path the REST routes use, so a card created over MCP is byte-identical to one
created over ``POST /api/cards`` — and an agent moving a card to ``ready`` with
a tier dispatches it into the factory (RFC-0019 §3.2) exactly as a human's PATCH
would.

Auth (three credentials, in precedence order):

1. ``CFACTORY_MCP_SECRET`` — the LEGACY full-scope bearer. Live in prod; a caller
   presenting it holds read AND write, exactly as before.
2. ``CFACTORY_API_KEYS`` — the same scoped keystore that gates ``/api`` and
   ``/connect`` (``"<key>:read;<key2>:read,write"``). A key may call a tool only
   if it carries that tool's declared scope. This is the additive part.
3. Nothing configured — DENIED. It used to be open; hanging write tools off a
   fail-open surface is how a backlog gets mutated by anyone. Set
   ``CFACTORY_MCP_DEV_OPEN=true`` for local dev to opt back into open mode.

Unregistered tool names default to WRITE, so a tool added without a scope entry
fails closed rather than inheriting read.

Configure in an MCP client:
    {
      "mcpServers": {
        "cfactory": {
          "type": "http",
          "url": "${CFACTORY_URL}/mcp",
          "headers": {"Authorization": "Bearer ${CFACTORY_MCP_TOKEN}"}
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import card_ops, git_config_ops
from .api_deps import action_transport_dep, cards_store_dep
from .audit import get_audit_store
from .auth import READ, WRITE, extract_key, get_keystore, secret_matches
from .capabilities import capability_matrix
from .card_ops import CardNotFoundError, StageRefusedError
from .cards import (
    CardCreate,
    CardStore,
    CardUpdate,
    DuplicateCardKeyError,
    DuplicateIssueRefError,
)
from .config import get_settings
from .copilot.anomalies import detect_anomalies
from .copilot.tools import rollups as compute_rollups
from .copilot.tools import summarize_timeline
from .credentials import CredentialError
from .enterprise import identity_dep
from .git_config import SUPPORTED_PROVIDERS, GitConfigError, GitConfigUpdate
from .git_connections import (
    GitConnectionCreate,
    GitConnectionUpdate,
    GitRepositoryCreate,
    GitRepositoryUpdate,
    GitResourceNotFoundError,
)
from .store import get_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["CFactory MCP"])

# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "cfactory_list_workitems",
        "description": (
            "List every unit of work threaded across the PARR pipeline, with each "
            "factory's current status. One row per correlation_key (GitHub issue #). "
            "Use this to see the whole pipeline at a glance."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_get_workitem",
        "description": (
            "Get the full cross-factory state for one unit of work: PFactory (plan), "
            "AIFactory (code), TFactory (test) slices plus the event timeline."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["correlation_key"],
            "properties": {
                "correlation_key": {
                    "type": "string",
                    "description": "The GitHub issue number / shared key threading the work.",
                }
            },
        },
    },
    {
        "name": "cfactory_get_timeline",
        "description": (
            "Get the ordered completion-event timeline for one unit of work — the "
            "sequence of plan/code/test transitions across factories."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["correlation_key"],
            "properties": {
                "correlation_key": {"type": "string"},
            },
        },
    },
    {
        "name": "cfactory_get_rollups",
        "description": (
            "Aggregate counts across the pipeline (how many units are planning / "
            "coding / testing / done / stuck) — the cockpit's column totals."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_get_anomalies",
        "description": (
            "Detected anomalies needing attention: stuck tasks, failed phases, "
            "handbacks awaiting a human, stalled hand-offs. Poll this to know when "
            "a unit of work needs the coder's attention."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Board tools (RFC-0019 Phase 2b) — the planning backlog, read AND write
# ---------------------------------------------------------------------------

_CARD_KEY_PROP = {"type": "string", "description": "Stable card id, e.g. 'FCT-42'."}

# The two ids the phase-8 git surface addresses, declared once so every tool
# describes them the same way (RFC-0020 §3.3).
_CONNECTION_ID_PROP = {
    "type": "integer",
    "description": "Connection id, from cfactory_list_git_connections.",
}
_REPOSITORY_ID_PROP = {
    "type": "integer",
    "description": "Repository id, from cfactory_list_git_connections or _list_git_repositories.",
}

# The fields an agent may edit on an existing card. Declared once and spliced
# into both the create and the update schema so the two can never drift.
_CARD_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "description": "One-line statement of intent."},
    "acceptance_criteria": {
        "type": "array",
        "items": {"type": "string"},
        "description": "What must be true for the card to be done.",
    },
    "tier": {
        "type": "string",
        "enum": ["low", "medium", "hard"],
        "description": "RFC-0011 difficulty tier, deciding which intake path builds it.",
    },
    "assignee": {
        "type": "string",
        "description": "Owner — a human handle or a factory runtime ('aifactory').",
    },
    "milestone": {"type": "string", "description": "Release / grouping this card belongs to."},
    "issue_ref": {
        "type": "string",
        "description": (
            "Adopt an EXISTING GitHub issue, as 'owner/repo#123' (RFC-0019 §3.5). Set "
            "this instead of letting the card open its own issue. Once set, GitHub's "
            "title, labels and open/closed state win over the card's on every sync."
        ),
    },
    "repository_id": {
        "type": ["integer", "null"],
        "description": (
            "Which of this tenant's configured repositories this card is for (from "
            "cfactory_list_git_repositories). Omit — or send null — for the tenant's "
            "default repository, which is what a board with one repository always means. "
            "Setting it decides which host the card's issue is opened on, which "
            "credential is used, and which AIFactory project its build lands in."
        ),
    },
}

_STATUS_PROP = {
    "type": "string",
    "enum": ["backlog", "ready", "in_progress", "blocked", "done"],
    "description": "The board column the card sits in.",
}

_PRIORITY_PROP = {
    "type": "integer",
    "description": "Lower sorts first, so 0 is the top of the backlog.",
}

BOARD_TOOLS: list[dict[str, Any]] = [
    {
        "name": "cfactory_card_sync_state",
        "description": (
            "How CURRENT the board is, per connected repository — read this before "
            "trusting the backlog. Each entry says when that repository's issues were "
            "last read successfully (last_polled_at), how far the incremental read has "
            "got (watermark_at), and whether it is stale (no successful read for more "
            "than two poll cadences). Reconciliation runs on a background poll, so the "
            "board is never live: an issue filed a moment ago may not be here yet. "
            "Stale, or poll.enabled false? Call cfactory_import_cards to sync now."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_list_cards",
        "description": (
            "List / search the planning backlog — this tenant's cards, highest priority "
            "first. Filter to one board column, release, owner, or difficulty tier. This "
            "is the same backlog a human sees in the cockpit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": _STATUS_PROP,
                "milestone": {"type": "string"},
                "assignee": {"type": "string"},
                "tier": {"type": "string", "enum": ["low", "medium", "hard"]},
            },
        },
    },
    {
        "name": "cfactory_get_card",
        "description": (
            "Get one planning card in full: title, acceptance criteria, status, priority, "
            "tier, assignee, milestone, and the correlation_key joining it to the PARR "
            "pipeline once it has entered the factory."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    {
        "name": "cfactory_card_comments",
        "description": (
            "Read the imported issue DISCUSSION on one card, oldest comment first. "
            "The card's description is only the issue body; for planning the decision "
            "usually lives in the thread, so read this before proposing what to build. "
            "Comments are stored and refreshed by the same background poll as the "
            "issues, so this never calls the git host. 'synced_at' null means the "
            "thread has never been read successfully — which is NOT the same as an "
            "empty list beside a timestamp, that being an issue with no discussion."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    {
        "name": "cfactory_create_card",
        "description": (
            "Add a card to the planning backlog. Omit card_key to have the next "
            "'FCT-<n>' assigned. Use this to plan work into the board rather than "
            "filing a bare GitHub issue. Creating it straight into 'ready' with a tier "
            "dispatches it into the factory immediately, same as a later promotion."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "card_key": {
                    "type": "string",
                    "description": "Optional explicit id; auto-assigned when omitted.",
                },
                **_CARD_FIELDS,
                "status": _STATUS_PROP,
                "priority": _PRIORITY_PROP,
                "correlation_key": {
                    "type": "string",
                    "description": "RFC-0001 correlation key, once the card enters the factory.",
                },
            },
        },
    },
    {
        "name": "cfactory_update_card",
        "description": (
            "Edit an existing card's content: title, acceptance criteria, tier, assignee, "
            "milestone. Only the fields you pass are changed. To change the board column "
            "use cfactory_move_card; to change ordering use cfactory_reprioritise_card."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP, **_CARD_FIELDS},
        },
    },
    {
        "name": "cfactory_move_card",
        "description": (
            "Move a card between board columns (backlog / ready / in_progress / blocked / "
            "done) — the programmatic equivalent of dragging it in the cockpit. Moving a "
            "card to 'ready' WITH a tier set is the intake trigger: it dispatches into the "
            "factory (low/medium build in AIFactory, hard plans in PFactory first) and "
            "comes back joined to a work item. Dispatching twice is not possible."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key", "status"],
            "properties": {"card_key": _CARD_KEY_PROP, "status": _STATUS_PROP},
        },
    },
    {
        "name": "cfactory_reprioritise_card",
        "description": (
            "Change a card's priority, which is what orders the backlog (lower first)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key", "priority"],
            "properties": {"card_key": _CARD_KEY_PROP, "priority": _PRIORITY_PROP},
        },
    },
    {
        "name": "cfactory_sync_card_github",
        "description": (
            "Sync a card with its GitHub issue. Opens one if the card has none, or adopts "
            "and mirrors the issue named by issue_ref — syncing twice never opens a "
            "duplicate. GitHub is the record of truth: on conflict the ISSUE's title, "
            "labels and open/closed state overwrite the card's, while the card's own "
            "planning fields (priority, tier, milestone, acceptance criteria) are left "
            "alone. Use this to check whether work moved on GitHub. If GitHub is "
            "unreachable the reason is recorded on the card and ok is false."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    # ── Stage actions (RFC-0020 §3.7) ────────────────────────────────────────
    # The explicit counterpart to tier routing: say plan / code / test outright,
    # or run the sequence. Each is the MCP twin of
    # POST /api/cards/{card_key}/actions/<stage>, and the parity gate
    # (tests/test_board_parity.py) fails the build if one exists without the other.
    {
        "name": "cfactory_plan_card",
        "description": (
            "Send a card to PFactory for planning. Explicit, so it OVERRIDES tier routing "
            "for the destination — a 'low' card can be planned even though its tier would "
            "skip planning — while the tier still supplies the payload. Refuses (rather "
            "than dispatching into nothing) when the card has no tier or the plan stage is "
            "already running; a plan that already completed is skipped, not re-run."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    {
        "name": "cfactory_code_card",
        "description": (
            "Send a card to AIFactory to be built. Allowed on a 'hard' card that was never "
            "planned — the result warns that a stage was skipped rather than refusing. "
            "Refuses when the card has no tier (no payload to build) or no intake project "
            "is configured; a build that already completed is skipped, not re-run."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    {
        "name": "cfactory_test_card",
        "description": (
            "Send a card to TFactory to be verified against its acceptance criteria. "
            "Refuses with 'no_build_to_verify' unless the card is joined to a work item AND "
            "its code stage completed — verifying something that was never built would "
            "generate lanes against nothing."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    {
        "name": "cfactory_run_card",
        "description": (
            "Run a card through plan -> code -> test. Only the first stage still owed is "
            "dispatched now; each later one goes out when the previous reaches terminal "
            "success. Stages already complete are skipped, so this RESUMES a part-finished "
            "or failed card rather than restarting it, and a failed stage stops the "
            "sequence with the card blocked and the reason recorded."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    {
        "name": "cfactory_import_cards",
        "description": (
            "Import the connected repository's EXISTING issues into the planning backlog "
            "— the way a board gets populated from a repo that already has a backlog. "
            "Works on GitHub, GitLab and Azure DevOps alike. Imported cards land in "
            "'backlog' (closed issues in 'done') and NEVER in 'ready', so importing a "
            "repo does not dispatch a build per issue. Re-running never duplicates: it "
            "updates the cards it already created. Pull requests are never imported. "
            "Incremental by default — pass full=true to re-read every issue. NOT live: "
            "this is a poll, so an issue filed since the last run appears on the next one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": (
                        "Project path to import from ('owner/repo'); defaults to the "
                        "tenant's default repository."
                    ),
                },
                "repository_id": {
                    "type": "integer",
                    "description": (
                        "Import from THIS configured repository (from "
                        "cfactory_list_git_repositories) — which also decides the connection, "
                        "and therefore the host and credential, used to read it."
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": "Ignore the last-synced watermark and re-read every issue.",
                },
            },
        },
    },
    {
        "name": "cfactory_delete_card",
        "description": "Remove a card from the planning backlog for good.",
        "inputSchema": {
            "type": "object",
            "required": ["card_key"],
            "properties": {"card_key": _CARD_KEY_PROP},
        },
    },
    # ── Tenant git configuration (RFC-0020 §3.3) ─────────────────────────────
    # Which host the board syncs with, which project, and which AIFactory project
    # its builds land in. No tenant argument on purpose: a tool operates on the
    # CALLER's tenant (resolved from X-Tenant-Id exactly as the card tools are),
    # so there is no way to name somebody else's — isolation by construction
    # rather than by a second check that could be forgotten.
    {
        "name": "cfactory_get_git_config",
        "description": (
            "Read this tenant's git configuration: which provider (github / gitlab / "
            "azure_devops), which host, which project the board syncs cards with, which "
            "project issues are imported from, and which AIFactory project dispatched "
            "cards are built in. 'status' is derived: unconfigured (no project named), "
            "credential_missing (a project, but no usable credential on the deployment), "
            "configured (never proved), verified (proved by cfactory_verify_git_config). "
            "No credential is ever returned."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_set_git_config",
        "description": (
            "Replace this tenant's git configuration — the programmatic equivalent of the "
            "cockpit's Settings > Git integration panel. A FULL replacement: an omitted "
            "optional field is cleared. Saving clears any previous verification, because "
            "it proved a different configuration. Rejects a project path the provider "
            "cannot address and any 'factory:<tier>' default label (that label is the "
            "fleet's intake trigger — it would build the same card twice). Credentials "
            "are NOT set here; they stay deployment-level until RFC-0020 phase 3."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": list(SUPPORTED_PROVIDERS),
                    "description": "Which git host. Defaults to github.",
                },
                "base_url": {
                    "type": "string",
                    "description": (
                        "Host root, for a self-hosted GitLab / GitHub Enterprise / Azure "
                        "DevOps Server. Omit for the provider's public default."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": (
                        "The project the board syncs with: 'owner/repo' on GitHub, a "
                        "group path on GitLab, 'organization/project/repo' on Azure DevOps."
                    ),
                },
                "intake_project": {
                    "type": "string",
                    "description": (
                        "Optional: import issues from THIS project instead of 'project'. "
                        "Defaults to 'project'."
                    ),
                },
                "aifactory_project_id": {
                    "type": "string",
                    "description": (
                        "The AIFactory project id a dispatched card is BUILT in (an opaque "
                        "project uuid, not a repository path). Without it, low/medium cards "
                        "cannot be dispatched and the code/test stages refuse."
                    ),
                },
                "default_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels put on issues the board opens. No 'factory:*'.",
                },
            },
        },
    },
    {
        "name": "cfactory_verify_git_config",
        "description": (
            "Check that this tenant's git configuration actually reaches its project: one "
            "cheap authenticated read of the repository, proving the host resolves, the "
            "credential is accepted and the project is visible. Records the outcome, so "
            "status becomes 'verified' or keeps the failure reason. An unreachable host "
            "is ok=false with the reason, never an error."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ── Tenant git credential (RFC-0020 §3.4) ────────────────────────────────
    # Write-only, like the panel: there is no read tool, because there is no read
    # of a credential anywhere on this board. Whether one exists is reported by
    # cfactory_get_git_config's masked "credential" block.
    {
        "name": "cfactory_set_git_credential",
        "description": (
            "Store (or replace) the credential this tenant's board authenticates to its "
            "git provider with. WRITE-ONLY: it is encrypted at rest and no tool, endpoint "
            "or panel ever returns it; cfactory_get_git_config reports only WHETHER one "
            "is configured. Refused when the deployment has no credential encryption key, "
            "rather than stored unencrypted. Every later use of it is written to the "
            "audit chain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "credential": {
                    "type": "string",
                    "description": (
                        "Whichever credential the provider issues for API access. Sent "
                        "once, never read back."
                    ),
                }
            },
            "required": ["credential"],
        },
    },
    {
        "name": "cfactory_delete_git_credential",
        "description": (
            "Forget this tenant's git credential — the revocation path for one that has "
            "leaked or been rotated at the provider. Idempotent: removing one that is not "
            "there is not an error. The board keeps serving afterwards; its git status "
            "simply becomes credential_missing. Operates on the connection the tenant's "
            "default repository lives on; use cfactory_delete_git_connection_credential for "
            "a specific one."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_git_capabilities",
        "description": (
            "What the fleet can actually do on each git host (RFC-0020 §3.5) — READ THIS "
            "BEFORE choosing a provider, because the reduction is real and it is not "
            "recoverable by configuration. Board sync, RFC-0011 label intake and the PARR "
            "run are identical on github, gitlab and azure_devops. Two things are not: "
            "assign_to_user (delegating an issue to the host's own coding agent) is full on "
            "GitHub, PARTIAL on GitLab (a Duo Workflow that silently no-ops without a Duo "
            "entitlement and an OAuth-scoped credential) and absent on Azure DevOps; and "
            "enable_auto_merge — the RFC-0011 auto-merge-when-green path and the RFC-0009 "
            "merge gate — plus AIFactory's automatic PR are GitHub-shaped and raise or skip "
            "elsewhere. Returns each capability's per-provider level (full / partial / none) "
            "and the sentence explaining it. Static: it describes the provider layer, not "
            "this tenant's configuration."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ── Connections and repositories (RFC-0020 §3.3, phase 8) ────────────────
    # The two-level model that replaces the single configuration above. No tenant
    # argument on any of them, for the same reason: a tool operates on the
    # CALLER's tenant, so there is no way to name somebody else's.
    {
        "name": "cfactory_list_git_connections",
        "description": (
            "List this tenant's git CONNECTIONS, each with its repositories. A connection is "
            "a place the board can authenticate to (provider github / gitlab / azure_devops, "
            "a host, and a credential); a repository is something to work on through one (its "
            "project path, the project issues are imported from, and the AIFactory project "
            "its builds land in). A tenant may have many of both — repos on GitHub and on a "
            "self-hosted GitLab at the same time. 'default_repository_id' is the repository a "
            "card that names none resolves to. Each connection's 'status' is derived: "
            "unconfigured (no repositories), credential_missing (no usable credential, or one "
            "the host refused), configured (never proved), verified (proved by "
            "cfactory_verify_git_connection). No credential is ever returned."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cfactory_create_git_connection",
        "description": (
            "Add a git connection for this tenant: a provider and, for a self-hosted GitHub "
            "Enterprise / GitLab / Azure DevOps Server, its host. Refused when this tenant "
            "already has a connection to the same provider and host — one host is configured "
            "once, so that 'which credential reaches it' has one answer. Store the credential "
            "separately with cfactory_set_git_connection_credential; it is never accepted here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": list(SUPPORTED_PROVIDERS),
                    "description": "Which git host. Defaults to github.",
                },
                "base_url": {
                    "type": "string",
                    "description": (
                        "Host root, for a self-hosted instance. Omit for the provider's "
                        "public default."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Human name shown in the cockpit ('Work GitHub'). Defaults to the "
                        "provider name."
                    ),
                },
            },
        },
    },
    {
        "name": "cfactory_update_git_connection",
        "description": (
            "Change a connection's provider, host or label. A PATCH: only the fields you send "
            "are applied. Moving it to another provider or host CLEARS its verification, "
            "because that proved a connection this one no longer is; renaming it does not. The "
            "credential is untouched — it belongs to the connection, not to its host."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["connection_id"],
            "properties": {
                "connection_id": _CONNECTION_ID_PROP,
                "provider": {"type": "string", "enum": list(SUPPORTED_PROVIDERS)},
                "base_url": {"type": "string"},
                "label": {"type": "string"},
            },
        },
    },
    {
        "name": "cfactory_delete_git_connection",
        "description": (
            "Forget a connection, ITS REPOSITORIES and its credential — everything that hangs "
            "off it, since a repository cannot be reached without its host. Cards are NOT "
            "deleted: a card whose repository is gone falls back to the tenant's default "
            "repository, and if the default was on this connection the oldest remaining "
            "repository is promoted."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["connection_id"],
            "properties": {"connection_id": _CONNECTION_ID_PROP},
        },
    },
    {
        "name": "cfactory_verify_git_connection",
        "description": (
            "Check that one connection's host answers and its credential is accepted: one "
            "cheap authenticated read of one of its repositories (the tenant default when "
            "that is on this connection, otherwise its oldest). Records the outcome, so the "
            "connection's status becomes 'verified' or keeps the failure reason. An "
            "unreachable host is ok=false with the reason, never an error; a connection with "
            "no repositories yet is ok=false with status 'unconfigured', because there is "
            "nothing on it to read."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["connection_id"],
            "properties": {"connection_id": _CONNECTION_ID_PROP},
        },
    },
    {
        "name": "cfactory_set_git_connection_credential",
        "description": (
            "Store (or replace) the credential ONE connection authenticates with. WRITE-ONLY: "
            "it is encrypted at rest, sealed against this tenant AND this connection (so the "
            "stored record cannot be replayed onto another connection), and no tool, endpoint "
            "or panel ever returns it — cfactory_list_git_connections reports only WHETHER one "
            "is configured. Refused when the deployment has no credential encryption key, "
            "rather than stored unencrypted. Every later use of it is written to the audit "
            "chain."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["connection_id", "credential"],
            "properties": {
                "connection_id": _CONNECTION_ID_PROP,
                "credential": {
                    "type": "string",
                    "description": (
                        "Whichever credential the provider issues for API access. Sent once, "
                        "never read back."
                    ),
                },
            },
        },
    },
    {
        "name": "cfactory_delete_git_connection_credential",
        "description": (
            "Forget ONE connection's credential — the revocation path for one that has leaked "
            "or been rotated at the provider. Idempotent. The connection keeps its "
            "repositories and simply reads as credential_missing."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["connection_id"],
            "properties": {"connection_id": _CONNECTION_ID_PROP},
        },
    },
    {
        "name": "cfactory_list_git_repositories",
        "description": (
            "List this tenant's repositories as a flat list — everything a card can be "
            "dispatched to — optionally just one connection's. 'is_default' marks the one a "
            "card that names no repository resolves to."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "connection_id": {
                    "type": "integer",
                    "description": "Only this connection's repositories. Omit for all of them.",
                }
            },
        },
    },
    {
        "name": "cfactory_create_git_repository",
        "description": (
            "Add a repository to one of this tenant's connections: the project the board syncs "
            "cards with, optionally a different project to import issues from, and the "
            "AIFactory project id dispatched cards are built in. The FIRST repository a tenant "
            "has becomes its default whatever make_default says, because a tenant with "
            "repositories and no default would refuse every card that named none. Rejects a "
            "project path the connection's provider cannot address and any 'factory:<tier>' "
            "default label (that label is the fleet's intake trigger — it would build the same "
            "card twice)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["connection_id", "project"],
            "properties": {
                "connection_id": _CONNECTION_ID_PROP,
                "project": {
                    "type": "string",
                    "description": (
                        "'owner/repo' on GitHub, a group path on GitLab, "
                        "'organization/project/repo' on Azure DevOps."
                    ),
                },
                "intake_project": {
                    "type": "string",
                    "description": (
                        "Optional: import issues from THIS project instead of 'project'."
                    ),
                },
                "aifactory_project_id": {
                    "type": "string",
                    "description": (
                        "The AIFactory project id a card dispatched to this repository is "
                        "BUILT in (an opaque project uuid, not a repository path)."
                    ),
                },
                "default_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels put on issues the board opens. No 'factory:*'.",
                },
                "make_default": {
                    "type": "boolean",
                    "description": "Make this the repository a card that names none resolves to.",
                },
            },
        },
    },
    {
        "name": "cfactory_update_git_repository",
        "description": (
            "Change a repository's project, intake project, AIFactory project id or default "
            "labels. A PATCH: only the fields you send are applied, and sending null for "
            "intake_project or aifactory_project_id clears it."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["repository_id"],
            "properties": {
                "repository_id": _REPOSITORY_ID_PROP,
                "project": {"type": "string"},
                "intake_project": {"type": ["string", "null"]},
                "aifactory_project_id": {"type": ["string", "null"]},
                "default_labels": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "cfactory_delete_git_repository",
        "description": (
            "Forget a repository. Cards that pointed at it are NOT deleted — they fall back to "
            "the tenant's default repository, exactly like a card that never named one. If this "
            "was the default, the oldest remaining repository is promoted."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["repository_id"],
            "properties": {"repository_id": _REPOSITORY_ID_PROP},
        },
    },
    {
        "name": "cfactory_set_default_git_repository",
        "description": (
            "Make one repository this tenant's default: the one a card that names no repository "
            "resolves to, for syncing its issue, for importing, and for which AIFactory project "
            "its build lands in. A tenant has exactly one default, enforced by the database."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["repository_id"],
            "properties": {"repository_id": _REPOSITORY_ID_PROP},
        },
    },
]

MCP_TOOLS += BOARD_TOOLS


# The scope each tool requires. Kept beside the catalog rather than inside the
# tool dicts so ``tools/list`` stays byte-for-byte the MCP shape clients already
# parse. Reads declare READ; every board MUTATION declares WRITE, so a read-only
# key can enumerate and inspect the backlog but never change it.
TOOL_SCOPES: dict[str, str] = {
    "cfactory_list_workitems": READ,
    "cfactory_get_workitem": READ,
    "cfactory_get_timeline": READ,
    "cfactory_get_rollups": READ,
    "cfactory_get_anomalies": READ,
    "cfactory_list_cards": READ,
    "cfactory_card_sync_state": READ,
    "cfactory_get_card": READ,
    "cfactory_card_comments": READ,
    "cfactory_create_card": WRITE,
    "cfactory_update_card": WRITE,
    "cfactory_move_card": WRITE,
    "cfactory_reprioritise_card": WRITE,
    "cfactory_sync_card_github": WRITE,
    "cfactory_import_cards": WRITE,
    # Stage actions dispatch work into the factory, so every one is a WRITE.
    "cfactory_plan_card": WRITE,
    "cfactory_code_card": WRITE,
    "cfactory_test_card": WRITE,
    "cfactory_run_card": WRITE,
    "cfactory_delete_card": WRITE,
    # Tenant git configuration (RFC-0020 §3.3). Verify is a WRITE: it makes an
    # authenticated call to somebody's git host and records the result.
    "cfactory_get_git_config": READ,
    "cfactory_set_git_config": WRITE,
    "cfactory_verify_git_config": WRITE,
    # The credential (RFC-0020 §3.4). Both WRITE, and there is deliberately no
    # READ counterpart to scope: a read-scoped key cannot obtain a credential
    # because nothing can.
    "cfactory_set_git_credential": WRITE,
    "cfactory_delete_git_credential": WRITE,
    # The published capability matrix (RFC-0020 §3.5). READ, and deliberately not
    # gated any harder: "what will I lose by picking GitLab?" must be answerable
    # before anything is configured.
    "cfactory_git_capabilities": READ,
    # Connections and repositories (RFC-0020 §3.3, phase 8). Verify is a WRITE for
    # the same reason the single-config one is: it calls somebody's git host and
    # records the result.
    "cfactory_list_git_connections": READ,
    "cfactory_create_git_connection": WRITE,
    "cfactory_update_git_connection": WRITE,
    "cfactory_delete_git_connection": WRITE,
    "cfactory_verify_git_connection": WRITE,
    "cfactory_set_git_connection_credential": WRITE,
    "cfactory_delete_git_connection_credential": WRITE,
    "cfactory_list_git_repositories": READ,
    "cfactory_create_git_repository": WRITE,
    "cfactory_update_git_repository": WRITE,
    "cfactory_delete_git_repository": WRITE,
    "cfactory_set_default_git_repository": WRITE,
}


# ---------------------------------------------------------------------------
# Auth — scope model (RFC-0019 Phase 2a)
# ---------------------------------------------------------------------------

FULL_SCOPE = frozenset({READ, WRITE})


def _authenticate(request: Request) -> frozenset[str]:
    """Return the scopes the caller holds, or raise 401.

    Fails CLOSED: with no credential configured at all the request is denied
    unless ``CFACTORY_MCP_DEV_OPEN`` is explicitly set.
    """
    settings = get_settings()
    token = extract_key(request.headers.get("authorization"), request.headers.get("x-api-key"))

    # 1. Legacy full-scope bearer — unchanged behaviour for existing prod clients.
    if settings.mcp_secret and secret_matches(token, settings.mcp_secret):
        return FULL_SCOPE

    # 2. Scoped API keys — the same keystore that gates /api and /connect.
    keystore = get_keystore()
    if keystore.configured:
        scopes = keystore.scopes_for(token)
        if scopes is not None:
            return frozenset(scopes)

    # 3. Nothing matched. If something IS configured this is a bad token;
    #    if nothing is configured it is an unconfigured server (deny, not open).
    if settings.mcp_secret or keystore.configured:
        raise HTTPException(status_code=401, detail="Invalid MCP token")
    if settings.mcp_dev_open:
        return FULL_SCOPE
    raise HTTPException(
        status_code=401,
        detail=(
            "MCP is not configured: set CFACTORY_MCP_SECRET or CFACTORY_API_KEYS "
            "(or CFACTORY_MCP_DEV_OPEN=true for local dev)"
        ),
    )


def _require_tool_scope(tool_name: str, granted: frozenset[str]) -> None:
    """Raise 403 unless ``granted`` covers the scope ``tool_name`` declares.

    Unknown tools require WRITE — a tool added without a TOOL_SCOPES entry fails
    closed instead of silently inheriting read access.
    """
    required = TOOL_SCOPES.get(tool_name, WRITE)
    if required not in granted:
        raise HTTPException(status_code=403, detail=f"MCP token lacks required scope: {required!r}")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _service_summary(state: Any) -> dict[str, Any]:
    """Compact {status, phase, task_id} projection of a ServiceState."""
    return {
        "task_id": getattr(state, "task_id", None),
        "status": getattr(state, "status", None),
        "phase": getattr(state, "phase", None),
    }


def _tool_list_workitems() -> dict[str, Any]:
    items = get_store().list()
    rows = [
        {
            "correlation_key": wi.correlation_key,
            "title": wi.title,
            "pfactory": _service_summary(wi.pfactory),
            "aifactory": _service_summary(wi.aifactory),
            "tfactory": _service_summary(wi.tfactory),
            "events": len(wi.timeline),
        }
        for wi in items
    ]
    return {"count": len(rows), "items": rows}


def _tool_get_workitem(correlation_key: str) -> dict[str, Any]:
    wi = get_store().get(correlation_key)
    if wi is None:
        return {"error": f"no work item for {correlation_key!r}"}
    return wi.model_dump(mode="json")


def _tool_get_timeline(correlation_key: str) -> dict[str, Any]:
    summary = summarize_timeline(get_store(), correlation_key)
    if summary is None:
        return {"error": f"no work item for {correlation_key!r}"}
    return summary


def _tool_get_rollups() -> dict[str, Any]:
    return compute_rollups(get_store())


def _tool_get_anomalies() -> dict[str, Any]:
    found = detect_anomalies(get_store())
    return {"count": len(found), "anomalies": found}


# ---------------------------------------------------------------------------
# Board tool handlers — thin adapters over cfactory.card_ops
# ---------------------------------------------------------------------------

# Audit ``endpoint`` for a board mutation that arrived over MCP rather than REST,
# so the trail records which surface an agent used.
_MCP_ENDPOINT = "/mcp"


@dataclass(frozen=True)
class ToolContext:
    """Per-call state a board tool needs: the card store, the audit provenance,
    and the HTTP transport the RFC-0019 §3.2 intake dispatch goes out over.

    Resolved once in the endpoint from the request, so the handlers stay pure
    functions of (arguments, context) and never touch the Request.
    """

    cards: CardStore
    audit: card_ops.AuditContext
    transport: httpx.BaseTransport | None = None


def _tool_card_sync_state(_args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.sync_state(ctx.cards)


def _tool_list_cards(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.list_cards(
        ctx.cards,
        status=args.get("status"),
        milestone=args.get("milestone"),
        assignee=args.get("assignee"),
        tier=args.get("tier"),
    )


def _tool_get_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.get_card(ctx.cards, args.get("card_key", "")).model_dump(mode="json")


def _tool_card_comments(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.card_comments(ctx.cards, args.get("card_key", ""))


def _tool_create_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    card = card_ops.create_card(ctx.cards, ctx.audit, CardCreate(**args), transport=ctx.transport)
    return card.model_dump(mode="json")


def _tool_update_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Backs update_card, move_card AND reprioritise_card.

    All three are the same operation — a partial update — differing only in which
    field the agent-facing schema exposes. Splitting them in the catalogue makes
    the intent legible to an agent; splitting them here would just be three
    copies of one line. Mirrors REST, where all three are a PATCH.
    """
    fields = {k: v for k, v in args.items() if k != "card_key"}
    card = card_ops.update_card(
        ctx.cards,
        ctx.audit,
        args.get("card_key", ""),
        CardUpdate(**fields),
        transport=ctx.transport,
    )
    return card.model_dump(mode="json")


def _tool_sync_card_github(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.sync_card_github(
        ctx.cards, ctx.audit, args.get("card_key", ""), transport=ctx.transport
    )


def _tool_import_cards(args: dict[str, Any], ctx: ToolContext) -> Any:
    raw = args.get("repository_id")
    return card_ops.import_cards(
        ctx.cards,
        ctx.audit,
        project=args.get("project"),
        repository_id=_repository_id(args) if raw is not None else None,
        full=bool(args.get("full", False)),
        transport=ctx.transport,
    )


def _tool_delete_card(args: dict[str, Any], ctx: ToolContext) -> Any:
    return card_ops.delete_card(ctx.cards, ctx.audit, args.get("card_key", ""))


def _tool_get_git_config(_args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.get_git_config(ctx.cards)


def _tool_set_git_config(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.set_git_config(ctx.cards, ctx.audit, GitConfigUpdate(**args))


def _tool_verify_git_config(_args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.verify_git_config(ctx.cards, ctx.audit, transport=ctx.transport)


def _tool_set_git_credential(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.set_git_credential(
        ctx.cards, ctx.audit, str(args.get("credential") or "")
    )


def _tool_delete_git_credential(_args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.clear_git_credential(ctx.cards, ctx.audit)


# ── Connections and repositories (RFC-0020 §3.3, phase 8) ────────────────────
# Every one of these calls the SAME git_config_ops function its REST twin does,
# which is what makes parity a property rather than a coincidence.


def _connection_id(args: dict[str, Any]) -> int:
    """The connection id an argument names, or a 400-shaped refusal.

    MCP arguments are JSON from an agent, so the id is validated here rather than
    trusted — a string 'abc' must be a clean error, not a 500 from the store.
    """
    try:
        return int(args["connection_id"])
    except (KeyError, TypeError, ValueError):
        raise GitConfigError(
            "connection_id must be the integer id of one of your connections"
        ) from None


def _repository_id(args: dict[str, Any]) -> int:
    try:
        return int(args["repository_id"])
    except (KeyError, TypeError, ValueError):
        raise GitConfigError(
            "repository_id must be the integer id of one of your repositories"
        ) from None


def _tool_git_capabilities(_args: dict[str, Any], _ctx: ToolContext) -> Any:
    return capability_matrix().model_dump()


def _tool_list_git_connections(_args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.list_git_connections(ctx.cards)


def _tool_create_git_connection(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.create_git_connection(ctx.cards, ctx.audit, GitConnectionCreate(**args))


def _tool_update_git_connection(args: dict[str, Any], ctx: ToolContext) -> Any:
    fields = {k: v for k, v in args.items() if k != "connection_id"}
    return git_config_ops.update_git_connection(
        ctx.cards, ctx.audit, _connection_id(args), GitConnectionUpdate(**fields)
    )


def _tool_delete_git_connection(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.delete_git_connection(ctx.cards, ctx.audit, _connection_id(args))


def _tool_verify_git_connection(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.verify_git_connection(
        ctx.cards, ctx.audit, _connection_id(args), transport=ctx.transport
    )


def _tool_set_git_connection_credential(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.set_connection_credential(
        ctx.cards, ctx.audit, _connection_id(args), str(args.get("credential") or "")
    )


def _tool_delete_git_connection_credential(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.clear_connection_credential(ctx.cards, ctx.audit, _connection_id(args))


def _tool_list_git_repositories(args: dict[str, Any], ctx: ToolContext) -> Any:
    raw = args.get("connection_id")
    return git_config_ops.list_git_repositories(
        ctx.cards, _connection_id(args) if raw is not None else None
    )


def _tool_create_git_repository(args: dict[str, Any], ctx: ToolContext) -> Any:
    fields = {k: v for k, v in args.items() if k != "connection_id"}
    return git_config_ops.create_git_repository(
        ctx.cards, ctx.audit, _connection_id(args), GitRepositoryCreate(**fields)
    )


def _tool_update_git_repository(args: dict[str, Any], ctx: ToolContext) -> Any:
    fields = {k: v for k, v in args.items() if k != "repository_id"}
    return git_config_ops.update_git_repository(
        ctx.cards, ctx.audit, _repository_id(args), GitRepositoryUpdate(**fields)
    )


def _tool_delete_git_repository(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.delete_git_repository(ctx.cards, ctx.audit, _repository_id(args))


def _tool_set_default_git_repository(args: dict[str, Any], ctx: ToolContext) -> Any:
    return git_config_ops.set_default_git_repository(ctx.cards, ctx.audit, _repository_id(args))


def _tool_stage_card(stage: str) -> Callable[[dict[str, Any], ToolContext], Any]:
    """Handler for one stage tool — the whole sequence when ``stage`` is empty.

    Built per stage rather than written four times: the tools differ only in which
    stage they name, and going through ``card_ops`` is what makes them behave
    IDENTICALLY to their REST twins rather than merely similarly.
    """

    def handler(args: dict[str, Any], ctx: ToolContext) -> Any:
        card_key = args.get("card_key", "")
        if not stage:
            return card_ops.run_card_sequence(
                ctx.cards, ctx.audit, card_key, transport=ctx.transport
            )
        return card_ops.run_card_stage(
            ctx.cards, ctx.audit, card_key, stage, transport=ctx.transport
        )

    return handler


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], ToolContext], Any]] = {
    "cfactory_list_workitems": lambda _a, _c: _tool_list_workitems(),
    "cfactory_get_workitem": lambda args, _c: _tool_get_workitem(args.get("correlation_key", "")),
    "cfactory_get_timeline": lambda args, _c: _tool_get_timeline(args.get("correlation_key", "")),
    "cfactory_get_rollups": lambda _a, _c: _tool_get_rollups(),
    "cfactory_get_anomalies": lambda _a, _c: _tool_get_anomalies(),
    "cfactory_list_cards": _tool_list_cards,
    "cfactory_card_sync_state": _tool_card_sync_state,
    "cfactory_get_card": _tool_get_card,
    "cfactory_card_comments": _tool_card_comments,
    "cfactory_create_card": _tool_create_card,
    "cfactory_update_card": _tool_update_card,
    "cfactory_move_card": _tool_update_card,
    "cfactory_reprioritise_card": _tool_update_card,
    "cfactory_sync_card_github": _tool_sync_card_github,
    "cfactory_import_cards": _tool_import_cards,
    "cfactory_plan_card": _tool_stage_card("plan"),
    "cfactory_code_card": _tool_stage_card("code"),
    "cfactory_test_card": _tool_stage_card("test"),
    "cfactory_run_card": _tool_stage_card(""),
    "cfactory_delete_card": _tool_delete_card,
    "cfactory_get_git_config": _tool_get_git_config,
    "cfactory_set_git_config": _tool_set_git_config,
    "cfactory_verify_git_config": _tool_verify_git_config,
    "cfactory_set_git_credential": _tool_set_git_credential,
    "cfactory_delete_git_credential": _tool_delete_git_credential,
    "cfactory_git_capabilities": _tool_git_capabilities,
    "cfactory_list_git_connections": _tool_list_git_connections,
    "cfactory_create_git_connection": _tool_create_git_connection,
    "cfactory_update_git_connection": _tool_update_git_connection,
    "cfactory_delete_git_connection": _tool_delete_git_connection,
    "cfactory_verify_git_connection": _tool_verify_git_connection,
    "cfactory_set_git_connection_credential": _tool_set_git_connection_credential,
    "cfactory_delete_git_connection_credential": _tool_delete_git_connection_credential,
    "cfactory_list_git_repositories": _tool_list_git_repositories,
    "cfactory_create_git_repository": _tool_create_git_repository,
    "cfactory_update_git_repository": _tool_update_git_repository,
    "cfactory_delete_git_repository": _tool_delete_git_repository,
    "cfactory_set_default_git_repository": _tool_set_default_git_repository,
}


# How each transport-neutral domain error is rendered over JSON-RPC. A table
# rather than a ladder of ``except`` clauses because every entry says the same
# thing in the same shape, and the ladder grew one rung per phase.
_TOOL_ERRORS: tuple[tuple[type[Exception], Callable[[Any], dict[str, Any]]], ...] = (
    (CardNotFoundError, lambda exc: {"error": f"no card {exc.args[0]!r}"}),
    (DuplicateCardKeyError, lambda exc: {"error": f"card already exists: {exc.args[0]!r}"}),
    (
        DuplicateIssueRefError,
        lambda exc: {"error": f"another card already tracks issue {exc.args[0]!r}"},
    ),
    # The REST twin answers 409 {reason, message}; over JSON-RPC the same two
    # fields, so an agent can branch on the code and quote the sentence.
    (StageRefusedError, lambda exc: {"error": exc.message, "reason": exc.code}),
    # A rejected git configuration: 400 over REST, the same sentence here.
    (GitConfigError, lambda exc: {"error": str(exc)}),
    # A connection or repository this tenant does not have: 404 over REST. "Not
    # found" for another tenant's id too — a 403 would confirm that it exists.
    (GitResourceNotFoundError, lambda exc: {"error": str(exc)}),
    # A credential that could not be stored: 503 over REST, the same sentence
    # here. Its message names the misconfiguration and never the credential.
    (CredentialError, lambda exc: {"error": str(exc)}),
    (
        ValidationError,
        lambda exc: {"error": "invalid arguments", "details": exc.errors(include_url=False)},
    ),
)


def _dispatch_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
    """Run a tool, rendering the domain errors as JSON the agent can read.

    The REST routes turn these same errors into 404/409/422; over JSON-RPC the
    equivalent is an ``{"error": ...}`` payload, which is how the read tools
    already report a missing correlation_key.
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return handler(arguments, ctx)
    except tuple(kind for kind, _ in _TOOL_ERRORS) as exc:
        return next(render(exc) for kind, render in _TOOL_ERRORS if isinstance(exc, kind))


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 endpoint
# ---------------------------------------------------------------------------


def _result(result: Any, rpc_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(code: int, message: str, rpc_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_context(request: Request) -> ToolContext:
    """Resolve the caller and the stores a board tool operates on.

    Reuses the REST seams as plain functions rather than re-deriving any of
    them: ``identity_dep`` for the audit actor (the presented key, else
    "local"), ``cards_store_dep`` for the tenant-scoped card store — so an MCP
    write lands in the same partition an ``X-Tenant-Id``-bearing REST write
    would — and ``action_transport_dep`` for the intake dispatch's transport,
    the same seam ``/api/cards`` and ``/api/actions/execute`` go out over.
    """
    actor = identity_dep(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        get_keystore(),
    )
    return ToolContext(
        cards=cards_store_dep(request.headers.get("X-Tenant-Id")),
        audit=card_ops.AuditContext(get_audit_store(), actor, endpoint=_MCP_ENDPOINT),
        transport=action_transport_dep(),
    )


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """JSON-RPC 2.0 MCP endpoint. Handles initialize, tools/list, tools/call."""
    granted = _authenticate(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content=_error(-32700, "Parse error", None))

    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "initialize":
        return JSONResponse(
            _result(
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cfactory", "version": "1.0.0"},
                },
                rpc_id,
            )
        )

    if method == "tools/list":
        return JSONResponse(_result({"tools": MCP_TOOLS}, rpc_id))

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        _require_tool_scope(tool_name, granted)
        try:
            payload = _dispatch_tool(tool_name, arguments, _tool_context(request))
        except Exception:
            logger.exception("[cfactory-mcp] tool call failed tool=%s", tool_name)
            return JSONResponse(_error(-32603, "Internal error", rpc_id))
        return JSONResponse(
            _result(
                {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]},
                rpc_id,
            )
        )

    return JSONResponse(
        status_code=400, content=_error(-32601, f"Method not found: {method}", rpc_id)
    )

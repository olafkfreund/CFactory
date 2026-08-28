#!/usr/bin/env python3
"""Dump the backend HTTP contract the cockpit's zod schemas are written against.

WHY THIS EXISTS (Factory#1005). ``apps/frontend-web/src/api.ts`` is a
hand-maintained client: ~80 zod schemas describing what the backend returns,
with the static types inferred inward. The schemas validate at runtime, so a
drifted response is *rejected* rather than silently mis-cast -- but only in a
browser, only on the one endpoint a user happened to open, and only after the
change shipped. Nothing in CI noticed when a route was renamed or a response
field dropped. This closes that: the contract the client is written against is
committed to the repo, regenerated from the REAL app, and a `--check` run in CI
fails when the committed copy no longer matches what the backend produces.

WHAT IS COMMITTED (apps/frontend-web/src/api-contract.json):

* ``paths`` - every route the FastAPI app declares, path template -> methods,
  taken from ``app.openapi()`` in-process (no server to boot, no port to pick).
  A renamed or deleted route changes this map.
* ``responses`` - the actual JSON body each covered endpoint returns, captured
  through ``TestClient`` against a seeded in-memory store. This is the half that
  matters, and it is the half OpenAPI cannot give us here: not one handler in
  this backend declares a ``response_model`` (they all return
  ``dict[str, object]``), so the generated schema documents every response as an
  untyped object. Generating TypeScript types from it would produce ``unknown``
  for every field -- a gate that can never go red. Recording what the handlers
  really emit does not have that problem.

TWO HALVES, AND NEITHER ALONE IS THE GATE:

* The committed file itself is a golden artifact. `--check` diffs it, so a route
  rename, a route removal, a renamed field AND a backend ADDITION all show up as
  a red CI step with the offending lines printed.
* ``apps/frontend-web/src/apiContract.test.ts`` feeds each recorded body to the
  SAME zod schema the client uses, so drift the client would actually REJECT at
  runtime fails `npm test`.

The split matters because most of these schemas are strict ``z.object``, which
STRIPS unknown keys rather than rejecting them: a field the backend starts
sending is invisible to zod by construction. That is precisely how
``ServiceState.repo`` (Factory#218) became data the backend sent and the cockpit
silently dropped. The zod half can never catch that; the golden diff always does.

Usage:
    python scripts/dump_api_contract.py            # rewrite the committed file
    python scripts/dump_api_contract.py --check    # fail if it is stale (CI)
"""

# T201 (no print) targets SERVICE code, where stdout is not an output channel.
# This is a CI command-line tool whose product is what it prints: the staleness
# verdict and the offending diff have nowhere else to go. Scoped to the one rule
# for the same reason scripts/ratchet_lint.py scopes its copy.
# ruff: noqa: T201

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "apps" / "frontend-web" / "src" / "api-contract.json"

# Settings are read from the environment, and two of the recorded bodies embed
# them (/health lists the upstream URLs). A developer with CFACTORY_* exported
# would otherwise dump their own deployment's URLs and the gate would go red for
# everyone else. Cleared BEFORE cfactory.config is imported, since get_settings()
# caches. Import-time side effect, deliberately: there is no later hook.
for _name in [k for k in os.environ if k.startswith("CFACTORY_")]:
    del os.environ[_name]

sys.path.insert(0, str(REPO_ROOT / "apps" / "backend"))

# The dependency callables are imported from where they are DEFINED, not from
# cfactory.app which merely re-exports them: under mypy --strict an implicit
# re-export is an error, and these are the same function objects either way,
# which is what dependency_overrides keys on.
import httpx  # noqa: E402
from cfactory.adapters import AIFactoryAdapter, PFactoryAdapter, TFactoryAdapter  # noqa: E402
from cfactory.api_deps import adapters_dep, observe_transport_dep, store_dep  # noqa: E402
from cfactory.app import create_app  # noqa: E402
from cfactory.progress import LiveProgress, get_progress_hub, reset_progress_hub  # noqa: E402
from cfactory.store import WorkItemStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# The endpoints this gate covers, as (openapi path template, request path).
#
# Scope is the core WorkItem / progress / health surface the cockpit's main
# views depend on -- deliberately NOT all ~80 schemas (see the test file's
# "NOT COVERED" note). Every entry must be reachable without network egress and
# must produce a byte-stable body; that is why /api/cards, the git-connection
# surface and every write endpoint are absent rather than silently half-covered.
#
# Two endpoints the cockpit DOES call are deliberately absent:
#
#   /api/workitems/{k}/process -- its body is assembled from an upstream
#     service's own process payload, so a seeded record would be a fixture of
#     this script's invention rather than of the backend's behaviour, and what a
#     stubbed upstream produces is all-nulls: a body that parses against nearly
#     anything (see the vitest test's populated-fields assertion).
#   /api/anomalies -- its "stuck" detector compares the last event against
#     datetime.now() and renders the gap into a human string ("no progress for
#     ~Nh"), so a recorded body would change with the calendar. A golden file
#     that goes stale on its own is a gate people learn to ignore.
COVERED: tuple[tuple[str, str], ...] = (
    ("/health", "/health"),
    ("/api/services", "/api/services"),
    ("/api/workitems", "/api/workitems"),
    ("/api/workitems/{correlation_key}", "/api/workitems/100"),
    ("/api/rollups", "/api/rollups"),
    ("/api/tokens", "/api/tokens"),
    ("/api/tokens/by_worker", "/api/tokens/by_worker"),
    ("/api/progress", "/api/progress"),
    ("/api/tasks/{correlation_key}/worker-progress", "/api/tasks/100/worker-progress"),
    ("/api/tasks/{correlation_key}/cost-routing", "/api/tasks/100/cost-routing"),
)

# DELIBERATELY TINY, and every entry is justified below. Normalisation is this
# gate's own blind spot: a normalised value can no longer show a type change, so
# each key here is a field the gate has consciously stopped watching the VALUE
# of. Substitution preserves the JSON TYPE, so the shape a zod schema checks is
# untouched, and `null` is never substituted -- null-vs-string is a nullability
# difference, which is contract, not noise. Do not add a key here to silence a
# diff; a diff that keeps coming back is usually a real one.
#
#   created_at / updated_at ........ wall clock at ingest; differ every run.
#   last_activity_age_seconds ...... now() minus the above; same reason.
#   version ........................ the release version from cfactory.__init__;
#                                    it changes on every release bump, which is
#                                    not client drift.
VOLATILE_KEYS = frozenset({"created_at", "updated_at", "last_activity_age_seconds", "version"})
_CANONICAL_TIME = "1970-01-01T00:00:00Z"

# The seed event. One correlation key carried across all three services, with a
# usage + worker + routing block attached, so the recorded bodies exercise the
# nested schemas (TokenUsage, WorkerUsage, RoutingInfo, ServiceState, Liveness)
# rather than only the envelope. A bare event would record a body full of nulls
# and empty dicts, which parses against almost anything.
_SEED_KEY = "100"


def _seed_events() -> list[dict[str, Any]]:
    worker = {
        "worker_id": "w1",
        "subtask_id": "s1",
        "agent_phase": "coding",
        "provider": "anthropic",
        "model": "claude-opus-4",
        "input_tokens": 1200,
        "output_tokens": 340,
        "total_tokens": 1540,
        "cost_usd": 0.042,
        "duration_ms": 4200,
        "billing_mode": "subscription",
        "elapsed_ms": 4200,
    }
    # `usage.workers` is what store.py:_apply_terminal_or_scalar folds into the
    # slice's worker map, and that map is what /api/tokens/by_worker rolls up.
    # Without it that endpoint records three empty containers.
    usage = {
        "input_tokens": 1200,
        "output_tokens": 340,
        "total_tokens": 1540,
        "cost_usd": 0.042,
        "model": "claude-opus-4",
        "billing_mode": "subscription",
        "workers": [worker],
    }
    routing = {
        "routing_class": "standard",
        "tier": "medium",
        "tier_source": "router",
        "cost_estimate_usd": 0.05,
        "budget_mode": "balanced",
        "runtime": "nix",
        "phase_models": {"planning": "opus", "coding": "sonnet"},
    }
    common = {
        "correlation_key": _SEED_KEY,
        "updated_at": "2026-06-04T12:00:00Z",
        "usage": usage,
        "worker": worker,
    }
    return [
        {
            **common,
            "id": "seed-pfactory",
            "service": "pfactory",
            "task_id": "pfactory-t",
            "status": "planned",
            "phase": "plan",
            "routing": routing,
        },
        {
            **common,
            "id": "seed-aifactory",
            "service": "aifactory",
            "task_id": "aifactory-t",
            "status": "coding",
            "phase": "code",
        },
        {
            **common,
            "id": "seed-tfactory",
            "service": "tfactory",
            "task_id": "tfactory-t",
            "status": "triaged",
            "phase": "test",
        },
        # A Tier-1.5 heartbeat sample (store.py:_is_worker_progress_event needs
        # exactly `phase == "worker_progress"` plus a worker). Without one,
        # /api/tasks/{k}/worker-progress records `series: []` -- a body that
        # would parse against any schema at all, which is not coverage.
        {
            **common,
            "id": "seed-heartbeat",
            "service": "aifactory",
            "task_id": "aifactory-t",
            "status": "coding",
            "phase": "worker_progress",
        },
    ]


def _ok_transport() -> httpx.MockTransport:
    """A transport that answers every probe 200, so nothing leaves the process."""

    def ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(httpx.codes.OK, json={"sessions": [], "tasks": []})

    return httpx.MockTransport(ok)


def _stub_adapters() -> list[Any]:
    """Upstream adapters wired to a MockTransport, so /api/services is recorded
    without a single packet leaving the process. A real probe would make the
    dump depend on whether three other services happen to be running, which is
    the opposite of a stable committed artifact -- and an unreachable one is
    recorded with an error-reference id in `detail`, which is not stable either."""
    transport = _ok_transport()
    return [
        PFactoryAdapter("http://pfactory.invalid", transport=transport),
        AIFactoryAdapter("http://aifactory.invalid", transport=transport),
        TFactoryAdapter("http://tfactory.invalid", transport=transport),
    ]


def normalize(value: object, key: str | None = None) -> object:
    """Replace clock/version-derived values with a canonical one of the same JSON
    type, recursively. Everything else is passed through untouched."""
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, key) for v in value]
    if key in VOLATILE_KEYS and value is not None:
        if isinstance(value, str):
            return _CANONICAL_TIME
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return 0
    return value


def build_contract() -> dict[str, Any]:
    """Run the real app against a seeded temp store and record its contract."""
    reset_progress_hub()  # @cache'd process-wide; a stale hub would leak between runs
    previous_cwd = Path.cwd()
    try:
        return _record(previous_cwd)
    finally:
        # A TemporaryDirectory cannot clean itself up from inside, and leaving
        # the process in a deleted cwd breaks everything after it.
        os.chdir(previous_cwd)


def _record(previous_cwd: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        # Settings load from `.env` RELATIVE TO THE CWD (config.py's
        # SettingsConfigDict). A developer with a local .env at the repo root
        # would otherwise dump their own upstream URLs into /health's recorded
        # body and the gate would be red for everyone else. Running from an
        # empty directory is the same defence as clearing CFACTORY_* above; the
        # contract is written afterwards, through an absolute path.
        os.chdir(tmp)
        store = WorkItemStore(f"sqlite:///{Path(tmp) / 'contract.db'}")
        app = create_app()
        app.dependency_overrides[store_dep] = lambda: store
        app.dependency_overrides[adapters_dep] = _stub_adapters
        # The OpenObserve reachability probe is a SEPARATE transport from the
        # three factory adapters (api_deps.py:120) and would otherwise dial
        # localhost:5080 for real.
        app.dependency_overrides[observe_transport_dep] = _ok_transport
        # NOT `with TestClient(app)`: the context-manager form runs the app's
        # lifespan, which bootstraps the CONFIGURED database and starts the
        # upstream pollers. Both are exactly what a hermetic dump must not do --
        # it would write to (and re-CREATE tables in) whatever DB the local
        # environment points at, and poll three services over the network.
        # tests/conftest.py's `client` fixture omits it for the same reason.
        client = TestClient(app)
        for event in _seed_events():
            resp = client.post("/api/events", json=event)
            resp.raise_for_status()

        # The live-progress hub is filled by polling upstreams, which this dump
        # deliberately does not do. Seed it through the same model the route
        # serialises, so /api/progress records a real sample instead of the
        # empty list an unpolled hub returns.
        get_progress_hub().update(
            LiveProgress(
                correlation_key=_SEED_KEY,
                service="aifactory",
                phase="code",
                percent=42.0,
                subtask="s1",
                updated_at="2026-06-04T12:00:00Z",
            )
        )

        responses: dict[str, Any] = {}
        for template, path in COVERED:
            resp = client.get(path)
            if resp.status_code != httpx.codes.OK:
                msg = f"GET {path} returned HTTP {resp.status_code}: {resp.text[:300]}"
                raise SystemExit(msg)
            responses[f"GET {template}"] = normalize(resp.json())

        paths = {
            path: sorted(m for m in ops if m in _HTTP_METHODS)
            for path, ops in app.openapi()["paths"].items()
        }
        # Back out before the TemporaryDirectory removes itself under us.
        os.chdir(previous_cwd)

    reset_progress_hub()
    return {
        "//": (
            "GENERATED by scripts/dump_api_contract.py -- do not edit by hand. "
            "Regenerate after any backend route or response change; "
            "src/apiContract.test.ts parses these bodies with the client's own zod schemas."
        ),
        "paths": dict(sorted(paths.items())),
        "responses": responses,
    }


_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail (exit 1) if the committed contract is stale, instead of rewriting it",
    )
    args = parser.parse_args(argv)

    fresh = json.dumps(build_contract(), indent=2, sort_keys=False) + "\n"

    if not args.check:
        CONTRACT_PATH.write_text(fresh, encoding="utf-8")
        print(f"wrote {CONTRACT_PATH.relative_to(REPO_ROOT)}")
        return 0

    committed = CONTRACT_PATH.read_text(encoding="utf-8") if CONTRACT_PATH.exists() else ""
    if committed == fresh:
        print(f"{CONTRACT_PATH.relative_to(REPO_ROOT)} is up to date with the backend.")
        return 0

    diff = difflib.unified_diff(
        committed.splitlines(keepends=True),
        fresh.splitlines(keepends=True),
        fromfile="committed",
        tofile="regenerated",
    )
    print("".join(diff))
    print(
        "DRIFT: the backend's routes/responses no longer match the committed contract.\n"
        "Run `python scripts/dump_api_contract.py`, commit the result, and fix any\n"
        "apps/frontend-web/src/apiContract.test.ts failure the new contract exposes\n"
        "(that failure IS the client falling behind the backend)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""The served fleet aggregate validates against the RFC-0019 contract schema.

The shape tests in ``test_well_known.py`` assert the fields CFactory *means* to
emit; this one asserts the document is actually legal. That distinction was not
academic: an earlier revision passed every shape test while emitting a
services[] entry with no manifest body, which fails the `kind: fleet` branch and
— under `unevaluatedProperties: false` — cascades into every root key being
reported as unexpected.

Validated in each degradation state, because the degraded shapes are exactly the
ones a hand-written assertion tends to bless without checking:

- all three siblings reachable
- one down, two up
- all three down (only CFactory's own entry left in ``services``)

The schema is VENDORED at ``tests/data/agent-skills-manifest.schema.json`` —
byte-identical to Factory ``apis/agent-skills-manifest.schema.json`` @
8644e70c7b6728daa97c6abdd9d7ea4a7ed463da (olafkfreund/Factory#358, the PR that
adds ``unavailable[]``). Vendored rather than read from a sibling checkout so
this gate runs in CI, where the Factory repo is not present; re-vendor when that
PR merges. The same convention as the pinned ``factory-contracts`` copy — see
``test_factory_contracts_drift.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cfactory.app import create_app, fleet_transport_dep, store_dep
from cfactory.routes_well_known import FLEET_AGENT_SKILLS_PATH, reset_fleet_cache
from cfactory.store import WorkItemStore

# Sibling test module, same directory — pytest puts it on sys.path. Reuses the
# one MockTransport definition rather than restating the fleet's fake origins.
from test_well_known import _fleet_transport

jsonschema = pytest.importorskip("jsonschema")

_SCHEMA_PATH = Path(__file__).parent / "data" / "agent-skills-manifest.schema.json"

_ALL = {"pfactory", "aifactory", "tfactory"}


def _errors(doc: dict) -> list[str]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in validator.iter_errors(doc)
    ]


def _fleet(tmp_path, down: set[str]) -> dict:
    reset_fleet_cache()
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: WorkItemStore(f"sqlite:///{tmp_path / 't.db'}")
    app.dependency_overrides[fleet_transport_dep] = lambda: _fleet_transport(down)
    resp = TestClient(app).get(FLEET_AGENT_SKILLS_PATH)
    reset_fleet_cache()
    assert resp.status_code == 200  # degrading must never mean erroring
    body: dict = resp.json()
    return body


def test_schema_itself_is_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(
        json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    )


@pytest.mark.parametrize(
    "down",
    [set(), {"aifactory"}, _ALL],
    ids=["all-siblings-up", "one-sibling-down", "all-siblings-down"],
)
def test_served_fleet_aggregate_validates(tmp_path, down: set[str]) -> None:
    doc = _fleet(tmp_path, down)
    assert _errors(doc) == []
    assert doc["kind"] == "fleet"

    # Whatever is down, every services[] entry carries a usable manifest ...
    for entry in doc["services"]:
        assert entry["skills"] and entry["mcp"] and entry["openapi_url"]
    # ... and nothing is silently dropped: all four are accounted for.
    named = {e["service"]["name"] for e in doc["services"]} | {
        e["name"] for e in doc["unavailable"]
    }
    assert named == _ALL | {"cfactory"}
    assert {e["name"] for e in doc["unavailable"]} == down


def test_unavailable_entries_publish_nothing_beyond_the_configured_facts(tmp_path) -> None:
    """The reason is coarse by design — this endpoint is public, so upstream
    exception text must not ride out on it."""
    doc = _fleet(tmp_path, {"tfactory"})
    (entry,) = doc["unavailable"]
    assert entry == {
        "name": "tfactory",
        "title": "TFactory",
        "manifest_url": "http://localhost:3103/.well-known/agent-skills/index.json",
        "reason": "unreachable",
        "checked_at": entry["checked_at"],
    }
    assert entry["checked_at"].endswith("Z")


def test_a_sibling_serving_a_half_manifest_is_unavailable_not_folded_in(tmp_path) -> None:
    """A manifest missing required fields would poison services[]; it is reported
    as unavailable instead, and the aggregate stays valid."""
    reset_fleet_cache()
    app = create_app()
    app.dependency_overrides[store_dep] = lambda: WorkItemStore(f"sqlite:///{tmp_path / 'h.db'}")
    app.dependency_overrides[fleet_transport_dep] = lambda: httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"schema_version": "1", "kind": "service"})
    )
    doc = TestClient(app).get(FLEET_AGENT_SKILLS_PATH).json()
    reset_fleet_cache()

    assert _errors(doc) == []
    assert {e["reason"] for e in doc["unavailable"]} == {"manifest incomplete"}
    assert [e["service"]["name"] for e in doc["services"]] == ["cfactory"]

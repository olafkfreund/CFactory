"""Contract: the cockpit's build-stage reject is a valid AIFactory apply-correction (#498).

CFactory CI has no AIFactory checkout, so the triage schema is VENDORED at
``tests/data/aifactory-handback-triage.schema.json``. It is byte-identical to
AIFactory ``apps/backend/qa/handback_triage.schema.json`` @
74b4b9b9bed20c5d492d3e8a7b5067b0deb4bc2a. Re-vendor it when AIFactory changes the
contract, as for ``test_fleet_manifest_conformance.py``.

The request-model fields mirror AIFactory ``ApplyCorrectionRequest``
(apps/web-server/server/routes/execution.py) at the same SHA.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfactory.actions import propose_reject_review
from cfactory.models import Service
from test_actions import _seed

jsonschema = pytest.importorskip("jsonschema")

_SCHEMA = json.loads(
    (Path(__file__).parent / "data" / "aifactory-handback-triage.schema.json").read_text("utf-8")
)


@pytest.mark.parametrize("note", ["missing tests", None])
def test_reject_payload_satisfies_apply_correction_contract(store, note):
    _seed(store, service=Service.AIFACTORY, correlation_key="42", task_id="ai-7")
    payload = propose_reject_review(store, "42", note=note).payload

    # ApplyCorrectionRequest: fix_request_md is required; without confirm=true it only previews.
    assert isinstance(payload["fix_request_md"], str) and payload["fix_request_md"].strip()
    assert payload["confirm"] is True
    # APP_REQUIRE_CORRECTION_TRIAGE (default on since AIFactory 3.10.9): triage must be present.
    errors = [e.message for e in jsonschema.Draft202012Validator(_SCHEMA).iter_errors(payload["triage"])]
    assert errors == []

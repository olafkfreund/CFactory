"""The frontend-client drift gate itself (Factory#1005).

``scripts/dump_api_contract.py`` records what the real backend returns so
``apps/frontend-web/src/apiContract.test.ts`` can replay it through the
cockpit's own zod schemas. Two things about that script can fail quietly, so
both are asserted here rather than assumed:

* ``normalize`` is the gate's deliberate blind spot. It exists so a clock-derived
  value cannot make the committed file differ on every run, and it must therefore
  substitute a value of the SAME JSON TYPE and touch NOTHING else -- an over-broad
  normaliser would turn a real type change into a stable value and the gate would
  never see it.
* ``--check`` must actually return non-zero on a stale file. A drift gate that
  exits 0 either way is worse than no gate, because the green tick is read as
  evidence.
"""

from __future__ import annotations

import json

import dump_api_contract as dump


def test_normalize_substitutes_only_volatile_keys_and_keeps_the_json_type():
    body = {
        "created_at": "2026-06-04T12:00:00Z",  # volatile, string -> canonical string
        "last_activity_age_seconds": 12.5,  # volatile, number -> number
        "version": "9.9.9",  # volatile, string
        "status": "coding",  # NOT volatile: an enum the client checks
        "percent": 42.0,  # NOT volatile
        "nested": {"updated_at": "2026-06-04T12:00:00Z", "phase": "code"},
        "items": [{"updated_at": "2026-06-04T12:00:00Z", "task_id": "t1"}],
    }

    out = dump.normalize(body)

    assert out["created_at"] == "1970-01-01T00:00:00Z"
    assert isinstance(out["last_activity_age_seconds"], (int, float))
    assert out["last_activity_age_seconds"] == 0
    assert out["version"] == "1970-01-01T00:00:00Z"
    # Everything else must survive byte-for-byte, at every depth: these are the
    # values the zod schemas actually discriminate on.
    assert out["status"] == "coding"
    assert out["percent"] == 42.0
    assert out["nested"] == {"updated_at": "1970-01-01T00:00:00Z", "phase": "code"}
    assert out["items"] == [{"updated_at": "1970-01-01T00:00:00Z", "task_id": "t1"}]


def test_normalize_leaves_null_alone():
    """`null` vs a value is a NULLABILITY difference, which is contract, not
    noise -- substituting it would hide a field that stopped being populated."""
    assert dump.normalize({"updated_at": None, "created_at": None}) == {
        "updated_at": None,
        "created_at": None,
    }


def test_check_passes_on_the_committed_contract():
    """The committed file matches what the backend produces right now. This is
    the same assertion CI makes; having it here means a plain `pytest` run also
    catches a backend change that left the cockpit's contract behind."""
    assert dump.main(["--check"]) == 0


def test_check_fails_on_a_stale_contract(monkeypatch, tmp_path, capsys):
    """The half with teeth. A --check that cannot go red is decoration."""
    stale = tmp_path / "api-contract.json"
    stale.write_text(
        json.dumps({"//": "stale", "paths": {}, "responses": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dump, "CONTRACT_PATH", stale)

    assert dump.main(["--check"]) == 1
    assert "DRIFT" in capsys.readouterr().out

"""Store reconciliation: stale non-terminal stages are cleared when an upstream
no longer reports a task, so finished/removed tasks stop showing as running."""

from __future__ import annotations

from cfactory.models import Service, ServiceState
from cfactory.store import WorkItemStore


def _store():
    return WorkItemStore("sqlite://")  # in-memory


def test_reconcile_clears_stale_running_stage():
    s = _store()
    s.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="p:7", status="in_progress"))
    # Upstream now reports a different set — "p:7" is gone.
    cleared = s.reconcile_snapshot(Service.AIFACTORY, {"p:99"})
    assert cleared == 1
    assert s.get("7").aifactory.status is None


def test_reconcile_keeps_task_still_reported():
    s = _store()
    s.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="p:7", status="in_progress"))
    cleared = s.reconcile_snapshot(Service.AIFACTORY, {"p:7"})
    assert cleared == 0
    assert s.get("7").aifactory.status == "in_progress"


def test_reconcile_preserves_terminal_stage_even_if_absent():
    s = _store()
    s.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="p:7", status="done"))
    cleared = s.reconcile_snapshot(Service.AIFACTORY, set())  # not reported anymore
    assert cleared == 0
    assert s.get("7").aifactory.status == "done"  # completed history persists


def test_reconcile_keeps_review_only_if_still_reported():
    s = _store()
    s.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="p:7", status="human_review"))
    # Still reported → kept (awaiting a human, legitimately present).
    assert s.reconcile_snapshot(Service.AIFACTORY, {"p:7"}) == 0
    assert s.get("7").aifactory.status == "human_review"
    # No longer reported → gone → cleared.
    assert s.reconcile_snapshot(Service.AIFACTORY, set()) == 1
    assert s.get("7").aifactory.status is None


def test_reconcile_is_per_service():
    s = _store()
    s.upsert_snapshot("7", Service.AIFACTORY, ServiceState(task_id="ai:7", status="in_progress"))
    s.upsert_snapshot("7", Service.PFACTORY, ServiceState(task_id="pf:7", status="planning"))
    # Reconciling aifactory must not touch the pfactory stage.
    s.reconcile_snapshot(Service.AIFACTORY, set())
    wi = s.get("7")
    assert wi.aifactory.status is None
    assert wi.pfactory.status == "planning"


def test_prune_removes_orphan_duplicate_after_rekey():
    # An early poll keyed task 'p:6' under fallback 'af-6' (frozen failed); the
    # real GitHub-issue key '18' now reports the same task. The orphan must go.
    s = _store()
    s.upsert_snapshot("af-6", Service.AIFACTORY, ServiceState(task_id="p:6", status="failed"))
    s.upsert_snapshot("18", Service.AIFACTORY, ServiceState(task_id="p:6", status="in_progress"))
    affected = s.prune_duplicate_stages(Service.AIFACTORY, {"p:6": "18"})
    assert affected == 1
    assert s.get("af-6") is None          # orphan row deleted (no other stage)
    assert s.get("18").aifactory.status == "in_progress"  # canonical card kept


def test_prune_keeps_canonical_and_unrelated():
    s = _store()
    s.upsert_snapshot("18", Service.AIFACTORY, ServiceState(task_id="p:6", status="in_progress"))
    # canonical key matches -> nothing pruned
    assert s.prune_duplicate_stages(Service.AIFACTORY, {"p:6": "18"}) == 0
    assert s.get("18").aifactory.status == "in_progress"


def test_prune_only_clears_stage_when_other_services_present():
    s = _store()
    s.upsert_snapshot("af-6", Service.AIFACTORY, ServiceState(task_id="p:6", status="failed"))
    s.upsert_snapshot("af-6", Service.PFACTORY, ServiceState(task_id="pf:6", status="done"))
    s.upsert_snapshot("18", Service.AIFACTORY, ServiceState(task_id="p:6", status="in_progress"))
    assert s.prune_duplicate_stages(Service.AIFACTORY, {"p:6": "18"}) == 1
    row = s.get("af-6")
    assert row is not None and row.aifactory.status is None   # ai stage cleared
    assert row.pfactory.status == "done"                       # row kept (pf remains)

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

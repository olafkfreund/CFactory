"""Dispatch reservation and relay behavior for investigation cards (#561)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import httpx
import pytest
from cfactory import auth, config, investigations
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.card_ops import AuditContext
from cfactory.cards import CardCreate, CardStore
from cfactory.git_connections import GitConnectionCreate, GitRepositoryCreate, GitRepositoryUpdate
from fastapi.testclient import TestClient

_KEY = "k" * 32
_SHA = "a" * 40


class Upstream:
    def __init__(self) -> None:
        self.compare = "identical"
        self.compare_status = 200
        self.start_status = 200
        self.start_body = {"id": "run-1", "state": "running"}
        self.calls: list[httpx.Request] = []
        self.on_compare = None
        self.on_start = None
        self.on_stop = None
        self.on_get = None
        self.run_body = {
            "id": "run-1",
            "state": "completed",
            "pods_gone": True,
            "usage": {"tokens": 1},
        }
        self.stop_status = 200
        self.relay_status = 200
        self.relay_body = {"detail": "busy"}

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        self.calls.append(request)
        path = request.url.path
        if "/compare/" in path:
            if self.on_compare:
                self.on_compare()
            return httpx.Response(self.compare_status, json={"status": self.compare})
        if path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": _SHA})
        if path == "/repos/acme/widgets":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/api/investigations" and request.method == "POST":
            if self.on_start:
                self.on_start()
            return httpx.Response(self.start_status, json=self.start_body)
        if path.endswith("/logs"):
            if self.relay_status != 200:
                return httpx.Response(self.relay_status, json=self.relay_body)
            return httpx.Response(200, json={"entries": [{"seq": 1, "line": "ok"}], "cursor": 1})
        if path.endswith("/lease"):
            if self.relay_status != 200:
                return httpx.Response(self.relay_status, json=self.relay_body)
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/stop"):
            if self.on_stop:
                self.on_stop()
            return httpx.Response(self.stop_status, json=self.relay_body if self.stop_status != 200 else {"state": "stopped", "pods_gone": False})
        if self.on_get:
            self.on_get()
        return httpx.Response(self.relay_status, json=self.relay_body if self.relay_status != 200 else self.run_body)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CFACTORY_INVESTIGATIONS", "on")
    config.reset_settings()
    auth.set_keys({_KEY: {"read", "write"}})
    store = CardStore(f"sqlite:///{tmp_path / 'cards.db'}")
    audit = AuditStore(f"sqlite:///{tmp_path / 'audit.db'}", hmac_secret="dispatch-test")  # noqa: S106
    upstream = Upstream()
    connection = store.create_connection(GitConnectionCreate(provider="github"))
    repo = store.create_repository(
        connection.id, GitRepositoryCreate(project="acme/widgets", aifactory_project_id="proj-1")
    )
    app = create_app()
    app.dependency_overrides[cards_store_dep] = lambda: store
    app.dependency_overrides[audit_dep] = lambda: audit
    app.dependency_overrides[action_transport_dep] = lambda: httpx.MockTransport(upstream)
    client = TestClient(app, headers={"Authorization": f"Bearer {_KEY}"})
    card = store.create_investigation_card(
        title="Investigate: why?",
        description="why?",
        repository_id=repo.id,
        binding={"repository_id": repo.id, "repo": repo.project, "aifactory_project_id": "proj-1"},
    )
    yield store, audit, upstream, client, card.card_key, repo.id
    auth.reset_keystore()
    config.reset_settings()


def _post(client, key, **changes):
    body = {"request": "why?", "sha": _SHA, "test_command": None, **changes}
    return client.post(f"/api/cards/{key}/actions/investigate", json=body)


def _reason(response):
    return response.json()["detail"]["reason"]


def test_dispatch_reservations_and_limits(env):
    store, _, upstream, client, key, repo_id = env
    for changes in (
        {"sha": "A" * 40},
        {"limits": {"minutes": True}},
        {"limits": {"minutes": 0}},
        {"limits": {"minutes": 31}},
        {"limits": {"unknown": 1}},
    ):
        assert _post(client, key, **changes).status_code == 422
    assert not any(r.url.path == "/api/investigations" for r in upstream.calls)
    response = _post(client, key, request="other")
    assert (response.status_code, _reason(response)) == (409, "request_changed")
    response = _post(client, key, test_command="pytest")
    assert (response.status_code, _reason(response)) == (409, "test_command_changed")
    upstream.compare = "ahead"
    assert _reason(_post(client, key)) == "sha_not_on_default_branch"
    upstream.compare = "identical"
    store.update_repository(repo_id, GitRepositoryUpdate(investigate_test_command="pytest"))
    assert _reason(_post(client, key)) == "test_command_changed"
    store.update_repository(repo_id, GitRepositoryUpdate(investigate_test_command=None))
    assert _post(client, key).json() == {"run_id": "run-1"}
    assert _reason(_post(client, key)) == "run_live"


@pytest.mark.parametrize("command", [None, "", "pytest -q"])
def test_matching_commands(env, command):
    store, _, upstream, client, key, repo_id = env
    store.update_repository(repo_id, GitRepositoryUpdate(investigate_test_command=command))
    assert _post(client, key, test_command=command).status_code == 200
    if command == "":
        sent = next(r for r in upstream.calls if r.url.path == "/api/investigations")
        assert json.loads(sent.content)["test_command"] is None


@pytest.mark.parametrize("suffix", ["", "/logs", "/lease", "/stop"])
@pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
def test_relay_4xx_preserves_upstream_detail_and_state(env, suffix, status):
    store, _, upstream, client, key, _ = env
    assert _post(client, key).status_code == 200
    before = store.get(key).stage_runs["investigate"]
    upstream.relay_status = upstream.stop_status = status
    path = f"/api/cards/{key}/investigate/run-1{suffix}"
    response = client.post(path) if suffix in ("/lease", "/stop") else client.get(path)
    assert response.status_code == 409
    assert response.json()["detail"] == {"reason": "upstream_refused", "detail": "busy"}
    assert store.get(key).stage_runs["investigate"] == before


def test_stop_refusal_never_mutates_stage_record(env):
    store, _, upstream, client, key, _ = env
    assert _post(client, key).status_code == 200
    before = store.get(key).stage_runs["investigate"]
    upstream.stop_status = 409
    upstream.relay_body = {"state": "stopped", "pods_gone": True, "detail": "busy"}
    client.post(f"/api/cards/{key}/investigate/run-1/stop")
    assert store.get(key).stage_runs["investigate"] == before


def test_recovery_is_local_and_work_card_cannot_relay(env):
    store, _, upstream, client, key, _ = env
    before = len(upstream.calls)
    assert client.get(f"/api/cards/{key}/investigate").status_code == 200
    assert len(upstream.calls) == before
    work = store.create(CardCreate(title="work"))
    assert client.get(f"/api/cards/{work.card_key}/investigate/run-1").status_code == 404
    assert len(upstream.calls) == before


def test_deleted_card_can_stop_and_pods_gone_stays_true(env):
    store, _, upstream, client, key, _ = env
    assert _post(client, key).status_code == 200
    base = f"/api/cards/{key}/investigate/run-1"
    assert client.get(base).status_code == 200
    store.delete(key)
    assert client.post(base + "/stop").status_code == 200
    record = store.get(key, include_deleted=True).stage_runs["investigate"]
    assert record["detail"]["pods_gone"] is True


def test_reservation_reads_repository_in_locked_session(env, monkeypatch):
    store, _, _, client, key, _ = env
    seen = []
    original = CardStore.locked_investigation_repository

    def tracked(session, repository_id):
        assert session.in_transaction()
        seen.append(repository_id)
        return original(session, repository_id)

    monkeypatch.setattr(CardStore, "locked_investigation_repository", staticmethod(tracked))
    assert _post(client, key).status_code == 200
    assert seen == [1]


def test_create_timeout_has_phase_limits(env, monkeypatch):
    _, _, _, client, key, _ = env
    original = investigations._aifactory
    seen = []

    def tracked(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(investigations, "_aifactory", tracked)
    assert _post(client, key).status_code == 200
    assert seen[0] == httpx.Timeout(connect=10, read=60, write=10, pool=10)


def test_long_run_id_audit_is_bounded(env):
    _, audit, upstream, client, key, _ = env
    upstream.start_body = {"id": "r" * 600}
    assert _post(client, key).status_code == 200
    entry = next(e for e in audit.list(limit=20) if e.kind == "investigate")
    assert len(entry.endpoint) == 512


def test_missing_long_card_key_audit_is_bounded(env):
    store, audit, upstream, _, _, _ = env
    with pytest.raises(investigations.InvestigationError):
        investigations.lease(
            store, AuditContext(audit, "tester"), config.get_settings(),
            "k" * 200, "run", httpx.MockTransport(upstream),
        )
    entry = next(e for e in audit.list(limit=20) if e.kind == "investigation_lease")
    assert len(entry.correlation_key) == 128
    assert len(entry.endpoint) <= 512


def test_run_id_is_quoted_in_all_relay_paths(env):
    store, audit, upstream, _, key, _ = env
    run_id = "part/other"
    with store.locked_investigation(key, "default") as (session, row):
        record = dict(row.stage_runs["investigate"])
        record["detail"] = {**record["detail"], "run_id": run_id}
        store.write_investigation(session, row, record)
    transport = httpx.MockTransport(upstream)
    settings = config.get_settings()
    ctx = AuditContext(audit, "tester")
    investigations.get_run(store, settings, key, run_id, transport)
    investigations.logs(store, settings, key, run_id, transport=transport)
    investigations.lease(store, ctx, settings, key, run_id, transport)
    investigations.stop(store, ctx, settings, key, run_id, transport)
    assert len([r for r in upstream.calls if b"part%2Fother" in r.url.raw_path]) == 4


@pytest.mark.parametrize("state", ["queued", "running", "completing", "uncertain", "completed"])
def test_recorded_run_blocks_redispatch_until_pods_gone(env, state):
    store, _, upstream, client, key, _ = env
    assert _post(client, key).status_code == 200
    upstream.run_body = {"state": state, "pods_gone": False}
    assert client.get(f"/api/cards/{key}/investigate/run-1").status_code == 200
    before = len([r for r in upstream.calls if r.url.path == "/api/investigations"])
    assert _reason(_post(client, key)) == "run_live"
    assert store.get(key).stage_runs["investigate"]["detail"]["run_id"] == "run-1"
    assert len([r for r in upstream.calls if r.url.path == "/api/investigations"]) == before
    upstream.run_body = {"state": "completed", "pods_gone": True}
    assert client.get(f"/api/cards/{key}/investigate/run-1").status_code == 200
    upstream.start_body = {"id": "run-2"}
    assert _post(client, key).json() == {"run_id": "run-2"}


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422, 503])
def test_start_refusal_keeps_reason_and_detail(env, status):
    _, _, upstream, client, key, _ = env
    upstream.start_status, upstream.start_body = status, {"detail": "busy"}
    response = _post(client, key)
    assert response.status_code == (503 if status == 503 else 409)
    assert response.json()["detail"] == {
        "reason": "investigations_draining" if status == 503 else "upstream_refused",
        **({} if status == 503 else {"detail": "busy"}),
    }


def test_reservation_rechecks_card_and_repository_after_compare(env):
    store, _, upstream, client, key, _ = env
    upstream.on_compare = lambda: store.delete(key)
    assert _reason(_post(client, key)) == "not_investigation_card"
    assert not any(r.url.path == "/api/investigations" for r in upstream.calls)


def test_repository_change_during_compare(env):
    store, _, upstream, client, key, repo_id = env
    upstream.on_compare = lambda: store.update_repository(
        repo_id, GitRepositoryUpdate(aifactory_project_id="other")
    )
    assert _reason(_post(client, key)) == "repository_changed"
    assert not any(r.url.path == "/api/investigations" for r in upstream.calls)


@pytest.mark.parametrize(
    ("compare", "http_status", "expected"),
    [
        ("identical", 200, 200),
        ("behind", 200, 200),
        ("ahead", 200, 409),
        ("diverged", 200, 409),
        ("", 404, 409),
        ("", 422, 409),
        ("", 500, 502),
    ],
)
def test_compare_results(env, compare, http_status, expected):
    _, _, upstream, client, key, _ = env
    upstream.compare, upstream.compare_status = compare, http_status
    assert _post(client, key).status_code == expected


def test_start_timeout_is_uncertain(env):
    store, _, upstream, client, key, _ = env

    def timeout():
        raise httpx.ReadTimeout("slow")

    upstream.on_start = timeout
    response = _post(client, key)
    assert (response.status_code, _reason(response)) == (504, "start_uncertain")
    assert store.get(key).stage_runs["investigate"]["status"] == "uncertain"


def test_two_dispatches_reserve_one_run(env):
    _, _, upstream, client, key, _ = env
    gate = Barrier(2)
    upstream.on_compare = lambda: gate.wait(timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_post, client, key) for _ in range(2)]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len([r for r in upstream.calls if r.url.path == "/api/investigations"]) == 1


@pytest.mark.parametrize(
    ("status", "body", "outcome", "code"),
    [
        (200, {"id": "run-1"}, "running", 200),
        (400, {"detail": "bad"}, "refused", 409),
        (503, {}, "refused", 503),
        (200, {}, "uncertain", 504),
        (500, {}, "uncertain", 504),
    ],
)
def test_outcome_table_and_deleted_card(env, status, body, outcome, code):
    store, audit, upstream, client, key, _ = env
    upstream.start_status, upstream.start_body = status, body
    upstream.on_start = lambda: store.delete(key)
    response = _post(client, key)
    assert response.status_code == code
    record = store.get(key, include_deleted=True).stage_runs["investigate"]
    assert record["status"] == outcome
    assert record["detail"]["run_id"] == ("run-1" if outcome == "running" else None)
    entry = next(entry for entry in audit.list(limit=20) if entry.kind == "investigate")
    assert entry.endpoint.startswith(f"card/{key}:attempt=")
    assert entry.endpoint.endswith(
        f";run={'run-1' if outcome == 'running' else '-'};outcome={outcome}"
    )
    assert entry.status_code == code


def test_relays_and_monotonic_state(env):
    store, audit, upstream, client, key, _ = env
    assert _post(client, key).status_code == 200
    base = f"/api/cards/{key}/investigate/run-1"
    assert client.get(base).json() == upstream.run_body
    assert client.get(base + "/logs").json()["entries"][0]["line"] == "ok"
    assert client.post(base + "/lease").json() == {"ok": True}
    assert (
        next(e for e in audit.list(limit=20) if e.kind == "investigation_lease").endpoint
        == f"card/{key}:run=run-1"
    )
    assert client.get(f"/api/cards/{key}/investigate").json()["detail"]["pods_gone"] is True
    upstream.run_body = {"state": "running", "pods_gone": False}
    client.get(base)
    record = store.get(key).stage_runs["investigate"]
    assert record["status"] == "completed"
    assert record["detail"]["pods_gone"] is True
    upstream.stop_status = 500
    assert _reason(client.post(base + "/stop")) == "stop_uncertain"
    assert (
        next(e for e in audit.list(limit=20) if e.kind == "investigation_stop").endpoint
        == f"card/{key}:run=run-1"
    )
    assert store.get(key).stage_runs["investigate"]["status"] == "completed"
    assert client.get(base.replace("run-1", "wrong")).status_code == 404
    store.delete(key)
    assert client.get(base).status_code == 200
    assert client.post(base + "/stop").status_code == 504


def test_stop_timeout_records_stopping(env):
    store, _, upstream, client, key, _ = env
    assert _post(client, key).status_code == 200

    def timeout():
        raise httpx.ReadTimeout("slow")

    upstream.on_stop = timeout
    response = client.post(f"/api/cards/{key}/investigate/run-1/stop")
    assert (response.status_code, _reason(response)) == (504, "stop_uncertain")
    assert store.get(key).stage_runs["investigate"]["status"] == "stopping"


def test_stop_timeout_uses_phase_limits(env, monkeypatch):
    _, _, _, client, key, _ = env
    assert _post(client, key).status_code == 200
    original = investigations._aifactory
    seen = []

    def tracked(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(investigations, "_aifactory", tracked)
    assert client.post(f"/api/cards/{key}/investigate/run-1/stop").status_code == 200
    assert seen == [httpx.Timeout(connect=10, read=45, write=10, pool=10)]


def test_slow_read_cannot_overwrite_new_run(env):
    store, _, upstream, client, key, _ = env
    assert _post(client, key).status_code == 200
    run_a = f"/api/cards/{key}/investigate/run-1"
    assert client.get(run_a).status_code == 200  # final, pods gone; another run may start
    entered, release = Event(), Event()

    def slow_get():
        entered.set()
        assert release.wait(10)

    upstream.on_get = slow_get
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.get, run_a)
        assert entered.wait(10)
        upstream.start_body = {"id": "run-2", "state": "running"}
        assert _post(client, key).json() == {"run_id": "run-2"}
        release.set()
        assert future.result().status_code == 200
    assert store.get(key).stage_runs["investigate"]["detail"]["run_id"] == "run-2"


def test_other_tenant_card_is_hidden_from_unscoped_store(env):
    store, _, _, client, _, repo_id = env
    other = store.scoped("other")
    other.create_investigation_card(
        title="Investigate: first",
        description="first",
        repository_id=repo_id,
        binding={
            "repository_id": repo_id,
            "repo": "acme/widgets",
            "aifactory_project_id": "proj-1",
        },
    )
    card = other.create_investigation_card(
        title="Investigate: private",
        description="private",
        repository_id=repo_id,
        binding={
            "repository_id": repo_id,
            "repo": "acme/widgets",
            "aifactory_project_id": "proj-1",
        },
    )
    assert _reason(_post(client, card.card_key, request="private")) == "not_investigation_card"
    assert client.get(f"/api/cards/{card.card_key}/investigate").status_code == 404


def test_stale_starting_can_be_replaced(env):
    store, _, _, client, key, _ = env
    with store.locked_investigation(key, "default") as (session, row):
        record = dict(row.stage_runs["investigate"])
        record["status"] = "starting"
        record["started_at"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
        store.write_investigation(session, row, record)
    assert client.get(f"/api/cards/{key}/investigate").json()["status"] == "uncertain"
    assert _post(client, key).status_code == 200


def test_fresh_starting_is_live(env):
    store, _, _, client, key, _ = env
    with store.locked_investigation(key, "default") as (session, row):
        record = dict(row.stage_runs["investigate"])
        record["status"] = "starting"
        record["started_at"] = datetime.now(UTC).isoformat()
        store.write_investigation(session, row, record)
    assert _reason(_post(client, key)) == "run_live"


def test_starting_at_old_120_second_limit_is_still_live(env):
    store, _, _, client, key, _ = env
    with store.locked_investigation(key, "default") as (session, row):
        record = dict(row.stage_runs["investigate"])
        record["status"] = "starting"
        record["started_at"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
        store.write_investigation(session, row, record)
    assert _reason(_post(client, key)) == "run_live"

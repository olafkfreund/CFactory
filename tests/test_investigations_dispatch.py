"""Dispatch reservation and relay behavior for investigation cards (#561)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import httpx
import pytest
from cfactory import auth, config
from cfactory.api_deps import action_transport_dep
from cfactory.app import audit_dep, cards_store_dep, create_app
from cfactory.audit import AuditStore
from cfactory.cards import CardStore
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
            return httpx.Response(200, json={"entries": [{"seq": 1, "line": "ok"}], "cursor": 1})
        if path.endswith("/lease"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/stop"):
            if self.on_stop:
                self.on_stop()
            return httpx.Response(self.stop_status, json={"state": "stopped", "pods_gone": False})
        if self.on_get:
            self.on_get()
        return httpx.Response(200, json=self.run_body)


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
    store, _, _, client, key, repo_id = env
    store.update_repository(repo_id, GitRepositoryUpdate(investigate_test_command=command))
    assert _post(client, key, test_command=command).status_code == 200


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
        record["started_at"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
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

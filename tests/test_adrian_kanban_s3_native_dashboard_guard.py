"""Native dashboard suppression when the Adrian authority is selected."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def dashboard_module():
    path = (
        Path(__file__).parents[1]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    name = "adrian_s3_native_dashboard_guard"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


@pytest.fixture()
def client(dashboard_module):
    app = FastAPI()
    app.include_router(dashboard_module.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _assert_suppressed(response) -> None:
    assert response.status_code == 409
    body = response.json()
    assert body["result"] == "REJECTED"
    assert body["state_changed"] is False
    assert isinstance(body["attempt_id"], str) and body["attempt_id"]
    assert body["boundary"] == {
        "from": "native-kanban-dashboard",
        "to": "adrian-kanban",
    }
    assert body["failed_checks"][0]["code"] == "NATIVE_SURFACE_SUPPRESSED"
    assert set(body["failed_checks"][0]) == {
        "code",
        "target",
        "expected",
        "observed",
        "accepted_format",
        "remediation",
        "responsible_actor",
        "retry",
    }
    assert body["not_evaluated_checks"] == []


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/api/plugins/kanban/tasks", {"json": {"title": "blocked"}}),
        ("patch", "/api/plugins/kanban/tasks/task-1", {"json": {"priority": 2}}),
        ("delete", "/api/plugins/kanban/tasks/task-1", {}),
        ("post", "/api/plugins/kanban/dispatch?dry_run=true", {}),
        (
            "post",
            "/api/plugins/kanban/tasks/task-1/home-subscribe/telegram",
            {},
        ),
    ],
)
def test_plugin_authority_suppresses_native_http_mutation_before_handler(
    dashboard_module, client, monkeypatch, method, path, kwargs
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "init_db",
        lambda *_args, **_kwargs: pytest.fail("native handler initialized its DB"),
    )

    response = getattr(client, method)(path, **kwargs)

    _assert_suppressed(response)


def test_authority_resolution_failure_suppresses_native_http_mutation(
    dashboard_module, client, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: (_ for _ in ()).throw(RuntimeError("bad config")),
    )
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "init_db",
        lambda *_args, **_kwargs: pytest.fail("native fallback initialized its DB"),
    )

    response = client.post(
        "/api/plugins/kanban/tasks", json={"title": "must fail closed"}
    )

    assert response.status_code == 409
    body = response.json()
    assert body["result"] == "REJECTED"
    assert body["state_changed"] is False
    assert body["failed_checks"][0]["code"] == "AUTHORITY_RESOLUTION_FAILED"


def test_native_authority_keeps_existing_dashboard_behavior(
    dashboard_module, client, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: "native",
    )

    response = client.post(
        "/api/plugins/kanban/tasks", json={"title": "native remains available"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["title"] == "native remains available"


@pytest.mark.asyncio
async def test_plugin_authority_closes_native_authoritative_event_socket(
    dashboard_module, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )

    class Socket:
        def __init__(self):
            self.closed = None
            self.accepted = False

        async def close(self, *, code):
            self.closed = code

        async def accept(self):
            self.accepted = True

    socket = Socket()
    await dashboard_module.stream_events(socket)

    assert socket.closed == 1008
    assert socket.accepted is False

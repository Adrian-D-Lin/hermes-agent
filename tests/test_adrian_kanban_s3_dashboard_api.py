"""Plugin-owned dashboard API delegates to the one command boundary."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_adrian_kanban_s1 import (  # noqa: F401
    adrian_plugin_modules,
    kanban_home,
)


@pytest.fixture()
def dashboard_module(adrian_plugin_modules):
    return importlib.import_module(
        f"{adrian_plugin_modules['package'].__name__}.dashboard.plugin_api"
    )


@pytest.fixture()
def client(dashboard_module):
    app = FastAPI()
    app.include_router(dashboard_module.router, prefix="/api/plugins/adrian-kanban")
    return TestClient(app)


def _accepted(operation: str, fields: dict, value: dict | None = None) -> dict:
    return {
        "result": "ACCEPTED",
        "state_changed": False,
        "attempt_id": fields["attempt_id"],
        "operation": operation,
        "value": value or {},
    }


def test_dashboard_manifest_declares_versioned_plugin_owned_surface(
    adrian_plugin_modules,
):
    root = Path(adrian_plugin_modules["package"].__path__[0])
    manifest = json.loads((root / "dashboard" / "manifest.json").read_text("utf-8"))

    assert manifest["name"] == "adrian-kanban"
    assert manifest["version"] == "0.2.0"
    assert manifest["api"] == "plugin_api.py"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["css"] == "dist/style.css"
    assert manifest["tab"] == {"path": "/adrian-kanban", "position": "after:skills"}


def test_handshake_reports_one_release_identity_and_selected_authority(
    dashboard_module, client, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )

    response = client.get("/api/plugins/adrian-kanban/handshake")

    assert response.status_code == 200
    body = response.json()
    assert body["authority"] == "adrian-kanban"
    assert body["mutation_controls_enabled"] is True
    versioning = importlib.import_module(
        dashboard_module.__package__.rsplit(".", 1)[0] + ".versioning"
    )
    assert body["versions"] == versioning.release_identity()


def test_board_projection_delegates_to_kanban_list(dashboard_module, client, monkeypatch):
    calls = []

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _accepted(operation, fields, {"initiatives": [], "tasks": []})

    monkeypatch.setattr(
        dashboard_module.kanban_db, "delegate_authority_operation", delegate
    )

    response = client.get(
        "/api/plugins/adrian-kanban/board?board=orchestrator"
        "&initiative_id=INIT-3&status=blocked&include_archived=true"
    )

    assert response.status_code == 200
    assert calls == [
        (
            "kanban_list",
            {
                "attempt_id": calls[0][1]["attempt_id"],
                "payload": {
                    "board": "orchestrator",
                    "initiative_id": "INIT-3",
                    "status": "blocked",
                    "include_archived": True,
                },
            },
        )
    ]
    assert response.json()["operation"] == "kanban_list"


@pytest.mark.parametrize(
    ("path", "operation", "payload"),
    [
        ("/tasks/task-4", "kanban_show", {"task_id": "task-4"}),
        (
            "/initiatives/INIT-4",
            "kanban_show",
            {"initiative_id": "INIT-4"},
        ),
        (
            "/tasks/task-4/attachments",
            "kanban_attachments",
            {"task_id": "task-4"},
        ),
    ],
)
def test_dashboard_read_routes_delegate_exact_projection(
    dashboard_module, client, monkeypatch, path, operation, payload
):
    calls = []

    def delegate(actual_operation, **fields):
        calls.append((actual_operation, fields))
        return _accepted(actual_operation, fields)

    monkeypatch.setattr(
        dashboard_module.kanban_db, "delegate_authority_operation", delegate
    )

    response = client.get(f"/api/plugins/adrian-kanban{path}")

    assert response.status_code == 200
    assert calls[0][0] == operation
    assert calls[0][1]["payload"] == payload


def test_dashboard_command_forwards_exact_boundary_fields_once(
    dashboard_module, client, monkeypatch
):
    calls = []

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _accepted(operation, fields, {"task_id": "task-5"})

    monkeypatch.setattr(
        dashboard_module.kanban_db, "delegate_authority_operation", delegate
    )
    request = {
        "attempt_id": "dashboard-attempt-5",
        "payload": {"task_id": "task-5", "board": "orchestrator"},
        "session_id": "desktop-session-2",
        "execution_context": "dashboard",
    }

    response = client.post(
        "/api/plugins/adrian-kanban/commands/kanban_block", json=request
    )

    assert response.status_code == 200
    assert calls == [("kanban_block", request)]


def test_dashboard_command_rejection_preserves_full_boundary_envelope(
    dashboard_module, client, monkeypatch
):
    rejection = {
        "result": "REJECTED",
        "state_changed": False,
        "attempt_id": "dashboard-denied-1",
        "operation": "kanban_block",
        "boundary": {"from": "adrian-kanban", "to": "adrian-kanban"},
        "failed_checks": [{"code": "ACTOR_NOT_AUTHORIZED"}],
        "not_evaluated_checks": [],
    }
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "delegate_authority_operation",
        lambda *_args, **_kwargs: rejection,
    )

    response = client.post(
        "/api/plugins/adrian-kanban/commands/kanban_block",
        json={"attempt_id": "dashboard-denied-1", "payload": {}},
    )

    assert response.status_code == 409
    assert response.json() == rejection


def test_dashboard_boundary_failure_is_fail_closed_without_native_fallback(
    dashboard_module, client, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "delegate_authority_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "init_db",
        lambda *_args, **_kwargs: pytest.fail("native fallback initialized its DB"),
    )

    response = client.get("/api/plugins/adrian-kanban/board")

    assert response.status_code == 503
    body = response.json()
    assert body["result"] == "REJECTED"
    assert body["state_changed"] is False
    assert body["failed_checks"][0]["code"] == "AUTHORITY_BOUNDARY_UNAVAILABLE"
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


def test_dashboard_command_boundary_exception_is_fail_closed(
    dashboard_module, client, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "delegate_authority_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    response = client.post(
        "/api/plugins/adrian-kanban/commands/kanban_block",
        json={"attempt_id": "dashboard-offline-2", "payload": {}},
    )

    assert response.status_code == 503
    assert response.json()["failed_checks"][0]["code"] == (
        "AUTHORITY_BOUNDARY_UNAVAILABLE"
    )


def test_dashboard_command_requires_a_json_object(client):
    response = client.post(
        "/api/plugins/adrian-kanban/commands/kanban_block", json=["not", "an", "object"]
    )

    assert response.status_code == 422

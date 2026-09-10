"""Plugin-owned dashboard API delegates to the one command boundary."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
def client(dashboard_module, monkeypatch):
    monkeypatch.setattr(
        dashboard_module,
        "mint_tailscale_authorizer_for_peer",
        lambda *_args, **_kwargs: object(),
        raising=False,
    )
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


def test_dashboard_api_loads_through_real_serve_standalone_spec_contract():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    module_name = "hermes_dashboard_plugin_adrian-kanban-regression"
    spec = importlib.util.spec_from_file_location(
        module_name,
        root / "dashboard" / "plugin_api.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)

        assert not module.__package__
        assert module.release_identity() == importlib.import_module(
            "plugins.adrian-kanban.versioning"
        ).release_identity()
        assert {route.path for route in module.router.routes} >= {
            "/handshake",
            "/board",
        }
    finally:
        sys.modules.pop(module_name, None)


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
    evidence = object()

    def mint(peer_host, **fields):
        assert peer_host == "testclient"
        assert fields["request_id"] == "dashboard-attempt-5"
        assert fields["ttl_seconds"] == 300
        assert fields["connection_id"].startswith("dashboard-http-")
        return evidence

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _accepted(operation, fields, {"task_id": "task-5"})

    monkeypatch.setattr(
        dashboard_module.kanban_db, "delegate_authority_operation", delegate
    )
    monkeypatch.setattr(
        dashboard_module, "mint_tailscale_authorizer_for_peer", mint
    )
    request = {
        "attempt_id": "dashboard-attempt-5",
        "payload": {"task_id": "task-5", "board": "orchestrator"},
        "session_id": "desktop-session-2",
        "execution_context": "caller-spoofed-context",
        "api_request_id": "caller-spoofed-request",
        "initial_authorizer": "caller-spoofed-authorizer",
        "override_now": 1,
    }

    response = client.post(
        "/api/plugins/adrian-kanban/commands/kanban_block", json=request
    )

    assert response.status_code == 200
    assert len(calls) == 1
    operation, fields = calls[0]
    assert operation == "kanban_block"
    assert fields == {
        "attempt_id": "dashboard-attempt-5",
        "payload": {"task_id": "task-5", "board": "orchestrator"},
        "session_id": "desktop-session-2",
        "execution_context": "dashboard",
        "api_request_id": "dashboard-attempt-5",
        "initial_authorizer": evidence,
        "override_now": fields["override_now"],
    }
    assert isinstance(fields["override_now"], int)
    assert fields["override_now"] > 0


def test_dashboard_command_rejects_non_tailscale_ingress_before_boundary(
    dashboard_module, client, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module,
        "mint_tailscale_authorizer_for_peer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("authenticated Tailscale peer required")
        ),
    )
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "delegate_authority_operation",
        lambda *_args, **_kwargs: pytest.fail("untrusted request reached boundary"),
    )

    response = client.post(
        "/api/plugins/adrian-kanban/commands/kanban_block",
        json={
            "attempt_id": "dashboard-untrusted-1",
            "payload": {"task_id": "task-5"},
            "human_authorizer": "adrian",
            "human_authn": "tailscale_ingress",
        },
        headers={"X-Tailscale-User-Login": "adrian@example.com"},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["result"] == "REJECTED"
    assert body["state_changed"] is False
    assert body["failed_checks"][0]["code"] == "ACTOR_NOT_AUTHORIZED"
    assert "Tailscale" in body["failed_checks"][0]["expected"]
    assert body["failed_checks"][0]["retry"] == "same_operation"


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


def _seed_notification_event(database_path: Path, schema_module) -> None:
    with sqlite3.connect(database_path) as conn:
        schema_module.create_schema(conn)
        response = json.dumps(
            {
                "result": "ACCEPTED",
                "state_changed": True,
                "attempt_id": "dashboard-event-attempt",
                "operation": "kanban_block",
                "value": {"task_id": "task-event", "blocked": True},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        records = json.dumps(
            [
                {
                    "card_type": "task",
                    "initiative_id": "initiative-event",
                    "task_id": "task-event",
                    "record_version": 4,
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO adrian_kanban_command_receipts "
            "(idempotency_key, operation, target, request_digest, "
            "response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "dashboard-event-receipt",
                "kanban_block",
                "task-event",
                "digest",
                response,
                123,
            ),
        )
        conn.execute(
            "INSERT INTO adrian_kanban_notification_outbox "
            "(idempotency_key, created_at, committed_records_json) "
            "VALUES (?, ?, ?)",
            ("dashboard-event-receipt", 123, records),
        )


def test_dashboard_event_stream_requires_canonical_websocket_auth(
    dashboard_module, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )
    stub = types.SimpleNamespace(_ws_auth_ok=lambda _ws: False)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)

    app = FastAPI()
    app.include_router(dashboard_module.router, prefix="/api/plugins/adrian-kanban")
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect(
            "/api/plugins/adrian-kanban/events"
        ):
            pass
    assert exc.value.code == 1008


def test_dashboard_event_stream_fails_closed_when_authority_resolution_fails(
    dashboard_module, monkeypatch
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: (_ for _ in ()).throw(RuntimeError("configuration unavailable")),
    )

    app = FastAPI()
    app.include_router(dashboard_module.router, prefix="/api/plugins/adrian-kanban")
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect(
            "/api/plugins/adrian-kanban/events"
        ):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("cursor", ["-1", "not-an-integer"])
def test_dashboard_event_stream_rejects_invalid_cursor(
    dashboard_module, monkeypatch, cursor
):
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )
    stub = types.SimpleNamespace(_ws_auth_ok=lambda _ws: True)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)

    app = FastAPI()
    app.include_router(dashboard_module.router, prefix="/api/plugins/adrian-kanban")
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect(
            f"/api/plugins/adrian-kanban/events?since={cursor}"
        ):
            pass
    assert exc.value.code == 1008


def test_dashboard_event_stream_projects_plugin_commits(
    dashboard_module, adrian_plugin_modules, tmp_path, monkeypatch
):
    database_path = tmp_path / "events.db"
    _seed_notification_event(database_path, adrian_plugin_modules["schema"])
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )
    monkeypatch.setattr(
        dashboard_module.kanban_db,
        "resolve_authority_path",
        lambda: str(database_path),
    )
    stub = types.SimpleNamespace(
        _ws_auth_ok=lambda ws: ws.query_params.get("token") == "event-secret"
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)

    app = FastAPI()
    app.include_router(dashboard_module.router, prefix="/api/plugins/adrian-kanban")
    with TestClient(app).websocket_connect(
        "/api/plugins/adrian-kanban/events?token=event-secret&since=0"
    ) as websocket:
        message = websocket.receive_json()

    assert message == {
        "events": [
            {
                "event_id": 1,
                "receipt_id": "dashboard-event-receipt",
                "operation": "kanban_block",
                "target": "task-event",
                "committed_at": 123,
                "committed_records": [
                    {
                        "card_type": "task",
                        "initiative_id": "initiative-event",
                        "task_id": "task-event",
                        "record_version": 4,
                    }
                ],
            }
        ],
        "cursor": 1,
    }

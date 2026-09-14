"""Public initiative updates obtain their exact WriteGate approval in-band."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_override_approval import evidence
from tests.test_adrian_kanban_s3_initiatives import (
    _BODY,
    _database,
    _seed_initiative,
    commands_module,  # noqa: F401
)


_UPDATED_BODY = _BODY.replace("- Complete delivery", "- Observe delivery in PC1")


def _normalizer(commands_module, path, provider):
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative,
        },
        state_resolver=lambda conn, operation, target, payload: conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND board_slug = ?",
            (target, payload["board"]),
        ).fetchone()[0],
        known_profiles={"default"},
    )
    return commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            "default",
        ),
    )


def _args():
    return {
        "initiative_id": "initiative-1",
        "update_kind": "body_update",
        "update": {"body": _UPDATED_BODY},
        "idempotency_key": "update-body-1",
    }


def _runtime():
    return {
        "session_id": "session-initiative",
        "turn_id": "turn-update-1",
        "api_request_id": "api-update-1",
        "user_task": "Update the initiative body after I approve the exact change.",
    }


@pytest.fixture
def public_update(commands_module, tmp_path, monkeypatch):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    approval_module = importlib.import_module(
        f"{commands_module.__package__}.initiative_update_approval"
    )
    monkeypatch.setattr(
        approval_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )
    return path, _normalizer(commands_module, path, provider), approval_module


def test_update_schema_allows_server_prepared_approval(commands_module):
    required = commands_module.TOOL_SCHEMAS["kanban_update_initiative"]["parameters"][
        "required"
    ]
    assert "approval_id" not in required
    assert (
        "approval_id"
        in commands_module.TOOL_SCHEMAS["kanban_update_initiative"]["parameters"][
            "properties"
        ]
    )


def test_public_update_prepares_approves_and_consumes_exact_request(
    public_update, monkeypatch
):
    path, normalizer, approval_module = public_update
    presented = []

    def approve(**request):
        presented.append(request)
        return {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-14T01:00:00Z",
            "approval_reference": "desktop-approval-1",
        }

    monkeypatch.setattr(approval_module, "request_write_gate_approval", approve)
    result = normalizer.submit(
        "kanban_update_initiative",
        _args(),
        _runtime(),
    )

    assert result["result"] == "ACCEPTED", result
    assert result["value"]["record_version"] == 1
    assert len(presented) == 1
    assert "Observe delivery in PC1" in presented[0]["command"]
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        card = conn.execute(
            "SELECT body, record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()
        approval = conn.execute(
            "SELECT request_id, operation, state, expected_version "
            "FROM write_gate_kanban_approvals"
        ).fetchone()
    assert tuple(card) == (_UPDATED_BODY, 1)
    assert approval["request_id"].startswith("kanban-update:turn-update-1:")
    assert approval["operation"] == "kanban_update_initiative"
    assert approval["state"] == "consumed"
    assert approval["expected_version"] == 0


def test_denied_public_update_leaves_initiative_unchanged(public_update, monkeypatch):
    path, normalizer, approval_module = public_update
    monkeypatch.setattr(
        approval_module,
        "request_write_gate_approval",
        lambda **request: {
            "approved": False,
            "decision": "deny",
            "decision_at": "2026-09-14T01:00:00Z",
        },
    )

    result = normalizer.submit(
        "kanban_update_initiative",
        _args(),
        _runtime(),
    )

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    with sqlite3.connect(path) as conn:
        card = conn.execute(
            "SELECT body, record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()
        state = conn.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0]
    assert card == (_BODY, 0)
    assert state == "cancelled"


def test_exact_replay_does_not_prompt_again(public_update, monkeypatch):
    path, normalizer, approval_module = public_update
    calls = []

    def approve(**request):
        calls.append(request["request_id"])
        return {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-14T01:00:00Z",
        }

    monkeypatch.setattr(approval_module, "request_write_gate_approval", approve)
    first = normalizer.submit("kanban_update_initiative", _args(), _runtime())
    second = normalizer.submit("kanban_update_initiative", _args(), _runtime())

    assert second == first
    assert len(calls) == 1
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "consumed"
        )

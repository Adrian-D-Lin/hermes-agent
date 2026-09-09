"""Command-context adapter for initiative override preparation and execution."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from tests.test_adrian_kanban_s3_initiatives import (
    _database,
    _seed_initiative,
    _seed_reconciliation,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_override_approval import evidence
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


def _binding():
    return SimpleNamespace(
        session_id="session-1",
        actor_profile="default",
        expected_version=0,
    )


def test_transition_handler_adapts_public_preparation_then_private_execution(
    commands_module, tmp_path, monkeypatch
):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    _seed_reconciliation(
        path,
        result_id="reconciliation-1",
        from_phase="D1",
        to_phase="DEV1",
    )
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    first = evidence("turn-1")
    conn.execute("BEGIN IMMEDIATE")
    prepare_context = commands_module._CommandContext(
        operation="kanban_transition_initiative",
        payload={
            "initiative_id": "initiative-1",
            "to_phase": "DEV1",
            "reconciliation_ref": "reconciliation-1",
            "gate_override": {
                "override_reason": "Adrian explicitly directed this nonstandard route."
            },
            "board": "orchestrator",
        },
        connection=conn,
        attempt_id="attempt-1",
        binding=_binding(),
        turn_id="turn-1",
        user_task="Prepare this exact route only; do not move until I approve.",
        idempotency_key="public-override-1",
        initial_authorizer=first,
        override_now=1010,
    )
    prepared = commands_module._handle_transition_initiative(prepare_context)
    conn.commit()
    assert set(prepared) == {
        "request_id",
        "approval_id",
        "canonical_payload",
        "canonical_digest",
    }
    assert prepared["request_id"] == "kanban-gate-override:turn-1"
    assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 1

    conn.execute("BEGIN IMMEDIATE")
    KanbanInitiativeApprovalHost(first).approve_distinct(
        conn,
        prepared["approval_id"],
        evidence("approval-click-1", 1011),
        expected_request_id=prepared["request_id"],
        expected_canonical_digest=prepared["canonical_digest"],
        approval_quote="Approve this exact override.",
        now=1020,
    )
    conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    execute_context = commands_module._CommandContext(
        operation="kanban_transition_initiative",
        payload={
            "initiative_id": "initiative-1",
            "board": "orchestrator",
            "_approved_gate_override_request_id": prepared["request_id"],
        },
        connection=conn,
        attempt_id="attempt-1:execute",
        binding=_binding(),
        idempotency_key="public-override-1:execute",
        override_now=1030,
    )
    result = commands_module._handle_transition_initiative(execute_context)
    conn.commit()

    assert result["result"] == "accepted_with_active_decision"
    assert result["record_version"] == 1
    assert conn.execute(
        "SELECT state FROM write_gate_kanban_approvals"
    ).fetchone()[0] == "consumed"
    conn.close()

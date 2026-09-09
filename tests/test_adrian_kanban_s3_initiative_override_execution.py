"""Atomic execution of an approved initiative-position gate override."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiative_override_preparation import _prepare
from tests.test_adrian_kanban_s3_initiatives import (
    _database,
    _seed_initiative,
    _seed_reconciliation,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_override_approval import evidence
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


@pytest.fixture
def execution_case(commands_module, tmp_path, monkeypatch):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    _seed_reconciliation(
        path,
        result_id="reconciliation-1",
        from_phase="D1",
        to_phase="DEV1",
    )
    module = importlib.import_module(
        f"{commands_module.__package__}.initiative_override"
    )
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    first = evidence("turn-1")
    prepared = _prepare(conn, module, initial_authorizer=first)
    conn.execute("BEGIN IMMEDIATE")
    KanbanInitiativeApprovalHost(first).approve_distinct(
        conn,
        prepared["approval_id"],
        evidence("approval-click-1", 1011),
        expected_request_id=prepared["request_id"],
        expected_canonical_digest=prepared["canonical_digest"],
        approval_quote="Approve this exact Kanban gate override.",
        now=1020,
    )
    conn.commit()
    yield conn, module, prepared
    conn.close()


def _execute(conn, module, prepared, *, now=1030):
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = module.execute_approved_initiative_transition_override(
            conn,
            request_id=prepared["request_id"],
            executor_session_id="human-session-1",
            executor_profile="default",
            mutation_id="override-mutation-1",
            idempotency_key="override-execution-key-1",
            now=now,
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
        return result


def test_approved_execution_moves_and_records_every_required_effect(execution_case):
    conn, module, prepared = execution_case

    result = _execute(conn, module, prepared)

    assert result == {
        "initiative_id": "initiative-1",
        "from_phase": "D1",
        "to_phase": "DEV1",
        "transition_id": 2,
        "record_version": 1,
        "request_id": prepared["request_id"],
        "result": "accepted_with_active_decision",
    }
    card = conn.execute(
        "SELECT record_version,closed_at FROM adrian_kanban_cards "
        "WHERE initiative_id='initiative-1'"
    ).fetchone()
    assert tuple(card) == (1, None)
    transition = conn.execute(
        "SELECT * FROM initiative_transitions WHERE initiative_id='initiative-1' "
        "ORDER BY transition_id DESC LIMIT 1"
    ).fetchone()
    assert transition["previous_transition_id"] == 1
    assert transition["transition_id"] == 2
    assert transition["from_phase"] == "D1"
    assert transition["to_phase"] == "DEV1"
    assert transition["trigger"] == "adrian_instruction"
    transition_payload = json.loads(transition["canonical_payload"])
    assert transition_payload["movement_basis"] == "adrian_gate_override"
    assert transition_payload["result"] == "accepted_with_active_decision"
    assert transition_payload["override_request_id"] == prepared["request_id"]
    override = conn.execute("SELECT * FROM gate_override_records").fetchone()
    assert override["request_id"] == prepared["request_id"]
    assert override["mutation_id"] == "override-mutation-1"
    assert override["movement_basis"] == "adrian_gate_override"
    assert override["result"] == "accepted_with_active_decision"
    assert override["override_reason"] == (
        "Adrian explicitly directed this nonstandard route."
    )
    assert json.loads(override["unsatisfied_gates"])[0]["code"] == "phase_close"
    decision = conn.execute("SELECT * FROM initiative_active_decisions").fetchone()
    assert decision["initiative_id"] == "initiative-1"
    assert decision["decision_kind"] == "adrian_gate_override"
    assert decision["authority_ref"] == prepared["request_id"]
    assert decision["active"] == 1
    comment = conn.execute("SELECT * FROM initiative_generated_comments").fetchone()
    assert comment["source_ref"] == prepared["request_id"]
    assert comment["body"] == (
        "Per Adrian's explicit instruction, initiative-1 moved from D1 to DEV1; "
        "gate criteria overridden, not satisfied."
    )
    approval = conn.execute(
        "SELECT * FROM write_gate_kanban_approvals WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()
    assert approval["state"] == "consumed"
    assert approval["consumed_mutation_id"] == "override-mutation-1"
    assert approval["consumed_idempotency_ref"] == "override-execution-key-1"


def test_closed_initiative_reopens_and_moves_in_same_approved_transaction(
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
    module = importlib.import_module(
        f"{commands_module.__package__}.initiative_override"
    )
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "UPDATE adrian_kanban_cards SET closed_at=1009 "
        "WHERE initiative_id='initiative-1'"
    )
    first = evidence("turn-1")
    prepared = _prepare(conn, module, initial_authorizer=first)
    conn.execute("BEGIN IMMEDIATE")
    KanbanInitiativeApprovalHost(first).approve_distinct(
        conn,
        prepared["approval_id"],
        evidence("approval-click-1", 1011),
        expected_request_id=prepared["request_id"],
        expected_canonical_digest=prepared["canonical_digest"],
        approval_quote="Approve reopening and moving this exact initiative.",
        now=1020,
    )
    conn.commit()

    _execute(conn, module, prepared)

    card = conn.execute(
        "SELECT record_version,closed_at FROM adrian_kanban_cards "
        "WHERE initiative_id='initiative-1'"
    ).fetchone()
    assert tuple(card) == (1, None)
    conn.close()


def test_stale_state_rejects_without_consuming_approval(execution_case):
    conn, module, prepared = execution_case
    conn.execute(
        "UPDATE adrian_kanban_cards SET record_version=1 "
        "WHERE initiative_id='initiative-1'"
    )

    with pytest.raises(ValueError, match="version"):
        _execute(conn, module, prepared)

    approval = conn.execute(
        "SELECT state,consumed_mutation_id FROM write_gate_kanban_approvals "
        "WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()
    assert tuple(approval) == ("approved", None)
    assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM initiative_active_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM initiative_generated_comments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 1


def test_late_write_failure_rolls_back_consumption_and_movement(execution_case):
    conn, module, prepared = execution_case
    conn.execute(
        "CREATE TRIGGER reject_override_comment BEFORE INSERT ON "
        "initiative_generated_comments BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        _execute(conn, module, prepared)

    assert conn.execute(
        "SELECT state FROM write_gate_kanban_approvals WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()[0] == "approved"
    assert tuple(
        conn.execute(
            "SELECT record_version,closed_at FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1'"
        ).fetchone()
    ) == (0, None)
    assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM initiative_active_decisions").fetchone()[0] == 0

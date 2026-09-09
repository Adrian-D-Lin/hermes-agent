"""Atomic execution of approved task-card gate overrides."""

from __future__ import annotations

import json

import pytest

from tests.test_adrian_kanban_s3_initiatives import commands_module  # noqa: F401
from tests.test_adrian_kanban_s3_override_approval import evidence
from tests.test_adrian_kanban_s3_task_override_derivation import task_override_case
from tests.test_adrian_kanban_s3_task_override_preparation import _prepare
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


def _approve(conn, prepared):
    first = evidence("task-override-turn-1")
    conn.execute("BEGIN IMMEDIATE")
    KanbanInitiativeApprovalHost(first).approve_distinct(
        conn,
        prepared["approval_id"],
        evidence("task-override-click-1", 1011),
        expected_request_id=prepared["request_id"],
        expected_canonical_digest=prepared["canonical_digest"],
        approval_quote="Approve this exact task gate override.",
        now=1020,
    )
    conn.commit()


def _execute(conn, module, prepared, *, now=1030):
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = module.execute_approved_task_status_override(
            conn,
            request_id=prepared["request_id"],
            executor_session_id="session-default",
            executor_profile="default",
            mutation_id="task-override-mutation-1",
            idempotency_key="task-override-execute-key-1",
            now=now,
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
        return result


def test_boolean_execution_time_is_rejected_without_consuming_approval(
    task_override_case,
):
    conn, module, _initiative_id, _run_id = task_override_case
    prepared = _prepare(conn, module)
    _approve(conn, prepared)

    with pytest.raises(ValueError, match="now must be a positive integer"):
        _execute(conn, module, prepared, now=True)

    assert conn.execute(
        "SELECT state FROM write_gate_kanban_approvals WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()[0] == "approved"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='task-parent'"
    ).fetchone()[0] == "running"


def test_approved_execution_closes_run_releases_child_and_records_override(
    task_override_case,
):
    conn, module, initiative_id, run_id = task_override_case
    prepared = _prepare(conn, module)
    _approve(conn, prepared)

    result = _execute(conn, module, prepared)

    assert result == {
        "task_id": "task-parent",
        "from_status": "running",
        "to_status": "done",
        "record_version": 1,
        "request_id": prepared["request_id"],
        "result": "accepted_with_active_decision",
    }
    task = conn.execute(
        "SELECT status,current_run_id,claim_lock,claim_expires,worker_pid,completed_at "
        "FROM tasks WHERE id='task-parent'"
    ).fetchone()
    assert tuple(task) == ("done", None, None, None, None, 1030)
    run = conn.execute(
        "SELECT status,outcome,ended_at,claim_lock,claim_expires,worker_pid "
        "FROM task_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert tuple(run) == ("released", "overridden", 1030, None, None, None)
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='task-child'"
    ).fetchone()[0] == "ready"
    assert conn.execute(
        "SELECT record_version FROM adrian_kanban_cards WHERE task_id='task-child'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT record_version FROM adrian_kanban_cards WHERE task_id='task-parent'"
    ).fetchone()[0] == 1

    record = conn.execute("SELECT * FROM task_gate_override_records").fetchone()
    assert record["request_id"] == prepared["request_id"]
    assert record["initiative_id"] == initiative_id
    assert record["task_id"] == "task-parent"
    assert record["from_status"] == "running"
    assert record["to_status"] == "done"
    assert record["movement_basis"] == "adrian_gate_override"
    assert record["result"] == "accepted_with_active_decision"
    assert json.loads(record["unsatisfied_gates"])[0]["code"] == (
        "accepted_reviewer_verdict"
    )
    decision = conn.execute("SELECT * FROM task_active_decisions").fetchone()
    assert decision["task_id"] == "task-parent"
    assert decision["authority_ref"] == prepared["request_id"]
    assert decision["active"] == 1
    comment = conn.execute(
        "SELECT author,body FROM task_comments WHERE task_id='task-parent' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(comment) == (
        "adrian-kanban",
        "Per Adrian's explicit instruction, task-parent moved from running to "
        "done; gate criteria overridden, not satisfied.",
    )
    child_event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id='task-child' "
        "AND kind='gate_override_dependency_release'"
    ).fetchone()
    assert json.loads(child_event["payload"])["override_request_id"] == prepared[
        "request_id"
    ]
    assert conn.execute("SELECT COUNT(*) FROM task_reviewer_verdicts").fetchone()[0] == 0
    approval = conn.execute(
        "SELECT state,consumed_mutation_id FROM write_gate_kanban_approvals "
        "WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()
    assert tuple(approval) == ("consumed", "task-override-mutation-1")


def test_stale_claim_rejects_without_consuming_or_moving(task_override_case):
    conn, module, _initiative_id, run_id = task_override_case
    prepared = _prepare(conn, module)
    _approve(conn, prepared)
    conn.execute(
        "UPDATE tasks SET claim_lock='new-claim' WHERE id='task-parent'"
    )
    conn.execute("UPDATE task_runs SET claim_lock='new-claim' WHERE id=?", (run_id,))
    conn.commit()

    with pytest.raises(ValueError, match="approved override|version|match"):
        _execute(conn, module, prepared)

    assert conn.execute(
        "SELECT state FROM write_gate_kanban_approvals WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()[0] == "approved"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='task-parent'"
    ).fetchone()[0] == "running"
    assert conn.execute("SELECT COUNT(*) FROM task_gate_override_records").fetchone()[0] == 0


def test_late_failure_rolls_back_consumption_and_every_task_effect(
    task_override_case,
):
    conn, module, _initiative_id, run_id = task_override_case
    prepared = _prepare(conn, module)
    _approve(conn, prepared)
    conn.execute(
        "CREATE TRIGGER reject_task_override_decision BEFORE INSERT ON "
        "task_active_decisions BEGIN SELECT RAISE(ABORT, 'test late failure'); END"
    )

    with pytest.raises(Exception, match="test late failure"):
        _execute(conn, module, prepared)

    assert conn.execute(
        "SELECT state FROM write_gate_kanban_approvals WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()[0] == "approved"
    assert tuple(
        conn.execute(
            "SELECT status,current_run_id,claim_lock FROM tasks WHERE id='task-parent'"
        ).fetchone()
    ) == ("running", run_id, "builder:claim")
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='task-child'"
    ).fetchone()[0] == "todo"
    assert conn.execute(
        "SELECT record_version FROM adrian_kanban_cards WHERE task_id='task-child'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_gate_override_records").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0


def test_changed_dependent_version_makes_approval_stale(task_override_case):
    conn, module, _initiative_id, _run_id = task_override_case
    prepared = _prepare(conn, module)
    _approve(conn, prepared)
    conn.execute(
        "UPDATE adrian_kanban_cards SET record_version=1 WHERE task_id='task-child'"
    )
    conn.commit()

    with pytest.raises(ValueError, match="approved override|version|match"):
        _execute(conn, module, prepared)

    assert conn.execute(
        "SELECT state FROM write_gate_kanban_approvals WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()[0] == "approved"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='task-parent'"
    ).fetchone()[0] == "running"

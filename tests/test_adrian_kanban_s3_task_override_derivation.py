"""Live derivation of task-card gate-override proposals (design v0.28 §7.13)."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_commands import (
    _insert_native_task,
    _insert_unified_card,
    _plugin_database,
    _runtime_modules,
    _seed_governed_running_task,
)
from tests.test_adrian_kanban_s3_initiatives import commands_module  # noqa: F401
from writegate.kanban_approvals import create_kanban_approval_schema


@pytest.fixture
def task_override_case(commands_module, tmp_path, monkeypatch):
    modules = _runtime_modules(commands_module)
    path, _ = _plugin_database(tmp_path, monkeypatch, modules["provider"])
    with sqlite3.connect(path) as schema_conn:
        create_kanban_approval_schema(schema_conn)
    task_id = "task-parent"
    run_id = _seed_governed_running_task(path, task_id=task_id)
    initiative_id = f"initiative-{task_id}"
    _insert_native_task(path, "task-child")
    _insert_unified_card(
        path,
        initiative_id=initiative_id,
        task_id="task-child",
        title="Dependent child",
    )
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tasks SET status='todo' WHERE id='task-child'")
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES "
            "('task-parent', 'task-child')"
        )
    module = importlib.import_module(f"{commands_module.__package__}.task_override")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn, module, initiative_id, run_id
    conn.close()


def _derive(conn, module, initiative_id, **changes):
    args = {
        "task_id": "task-parent",
        "board": "orchestrator",
        "to_status": "done",
        "override_reason": "Adrian explicitly directed this task movement.",
        "executor_session_id": "session-default",
        "executor_profile": "default",
    }
    args.update(changes)
    return module.derive_task_status_override(conn, **args)


def test_running_governed_task_derives_exact_close_and_dependency_release(
    task_override_case,
):
    conn, module, initiative_id, run_id = task_override_case

    proposal = _derive(conn, module, initiative_id)

    assert set(proposal) == {
        "operation",
        "initiative_id",
        "target",
        "target_kind",
        "board",
        "expected_version",
        "source",
        "destination",
        "gates",
        "non_bypassable_checks",
        "claim_run_closures",
        "dependency_changes",
        "downstream_exceptions",
        "reconciliation_ref",
        "override_reason",
        "commentary",
    }
    assert proposal["operation"] == "kanban_execute_task_gate_override"
    assert proposal["initiative_id"] == initiative_id
    assert proposal["target"] == "task-parent"
    assert proposal["target_kind"] == "task"
    assert proposal["expected_version"] == 0
    assert proposal["source"] == {
        "status": "running",
        "assignee": "builder",
        "current_run_id": run_id,
        "claim_lock": "builder:claim",
        "claim_expires": 1000,
        "worker_pid": None,
    }
    assert proposal["destination"] == {"status": "done"}
    assert {
        "code": "accepted_reviewer_verdict",
        "result": "unmet",
        "evidence_ref": None,
        "observed": "no accepted reviewer verdict authorizes completion",
    } in proposal["gates"]
    assert proposal["claim_run_closures"] == [
        {
            "task_id": "task-parent",
            "run_id": run_id,
            "run_status": "running",
            "outcome": "overridden",
            "claim_lock": "builder:claim",
        }
    ]
    assert proposal["dependency_changes"] == [
        {
            "task_id": "task-child",
            "action": "release",
            "from_status": "todo",
            "to_status": "ready",
            "expected_version": 0,
        }
    ]
    assert proposal["downstream_exceptions"] == []
    assert all(
        check["result"] == "met" for check in proposal["non_bypassable_checks"]
    )
    assert proposal["reconciliation_ref"] is None
    assert proposal["commentary"] == (
        "Per Adrian's explicit instruction, task-parent moved from running to "
        "done; gate criteria overridden, not satisfied."
    )


def test_backward_move_regates_idle_dependents_and_flags_active_ones(
    task_override_case,
):
    conn, module, initiative_id, _ = task_override_case
    conn.execute(
        "UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL WHERE id='task-parent'"
    )
    conn.execute("UPDATE tasks SET status='ready' WHERE id='task-child'")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES "
        "('task-active', 'Active dependent', 'running', 1000)"
    )
    conn.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type, initiative_id, task_id, title, created_at, board_slug) "
        "VALUES ('task', ?, 'task-active', 'Active dependent', 1000, 'orchestrator')",
        (initiative_id,),
    )
    conn.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES "
        "('task-parent', 'task-active')"
    )
    conn.commit()

    proposal = _derive(conn, module, initiative_id, to_status="review")

    assert proposal["dependency_changes"] == [
        {
            "task_id": "task-child",
            "action": "re_gate",
            "from_status": "ready",
            "to_status": "todo",
            "expected_version": 0,
        }
    ]
    assert proposal["downstream_exceptions"] == [
        {
            "task_id": "task-active",
            "status": "running",
            "expected_version": 0,
            "current_run_id": None,
            "claim_lock": None,
            "reason": "active_or_terminal_dependent_requires_explicit_disposition",
        }
    ]


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"executor_profile": "builder"}, "default"),
        ({"to_status": "running"}, "destination"),
        ({"to_status": "scheduled"}, "destination"),
        ({"to_status": "triage"}, "destination"),
        ({"to_status": "archived"}, "destination"),
        ({"to_status": "blocked"}, "destination"),
        ({"to_status": "running", "override_reason": " "}, "override_reason"),
    ],
)
def test_invalid_executor_or_destination_is_rejected(task_override_case, change, match):
    conn, module, initiative_id, _ = task_override_case
    with pytest.raises(ValueError, match=match):
        _derive(conn, module, initiative_id, **change)


def test_derivation_is_read_only(task_override_case):
    conn, module, initiative_id, _ = task_override_case
    before = conn.total_changes
    _derive(conn, module, initiative_id)
    assert conn.total_changes == before


def test_live_claim_is_part_of_stale_sensitive_derivation(task_override_case):
    conn, module, initiative_id, _ = task_override_case
    conn.execute("UPDATE tasks SET claim_lock='different-claim' WHERE id='task-parent'")
    conn.commit()

    with pytest.raises(ValueError, match="claim"):
        _derive(conn, module, initiative_id)


def test_non_governed_task_does_not_invent_a_reviewer_gate(
    task_override_case,
):
    conn, module, initiative_id, _ = task_override_case
    conn.execute("DELETE FROM task_handoff_requirements WHERE task_id='task-parent'")
    conn.commit()

    proposal = _derive(conn, module, initiative_id)

    assert all(gate["code"] != "accepted_reviewer_verdict" for gate in proposal["gates"])


@pytest.mark.parametrize("gap", ["wrong_board", "closed_task", "closed_parent"])
def test_task_and_open_parent_must_match_the_declared_board(
    task_override_case, gap
):
    conn, module, initiative_id, _ = task_override_case
    changes = {}
    if gap == "wrong_board":
        changes["board"] = "another-board"
    elif gap == "closed_task":
        conn.execute(
            "UPDATE adrian_kanban_cards SET closed_at=1001 WHERE task_id='task-parent'"
        )
    else:
        conn.execute(
            "UPDATE adrian_kanban_cards SET closed_at=1001 "
            "WHERE initiative_id=? AND task_id IS NULL",
            (initiative_id,),
        )
    conn.commit()

    with pytest.raises(ValueError):
        _derive(conn, module, initiative_id, **changes)

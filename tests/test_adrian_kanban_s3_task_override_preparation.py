"""Immutable preparation and durable record schema for task gate overrides."""

from __future__ import annotations

import json

from tests.test_adrian_kanban_s3_initiatives import commands_module  # noqa: F401
from tests.test_adrian_kanban_s3_override_approval import evidence
from tests.test_adrian_kanban_s3_task_override_derivation import task_override_case


def _prepare(conn, module, **changes):
    args = {
        "initial_authorizer": evidence("task-override-turn-1"),
        "initial_session_id": "session-default",
        "initial_message_id": "task-override-turn-1",
        "initial_quote": "Move task-parent to done despite the unmet review gate.",
        "executor_session_id": "session-default",
        "executor_profile": "default",
        "task_id": "task-parent",
        "board": "orchestrator",
        "to_status": "done",
        "override_reason": "Adrian explicitly directed this task movement.",
        "now": 1010,
        "expires_at": 1200,
    }
    args.update(changes)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = module.prepare_task_status_override(conn, **args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
        return result


def test_preparation_derives_and_stores_without_changing_task_state(
    task_override_case,
):
    conn, module, _initiative_id, run_id = task_override_case

    prepared = _prepare(conn, module)

    task = conn.execute(
        "SELECT status,current_run_id,claim_lock FROM tasks WHERE id='task-parent'"
    ).fetchone()
    assert tuple(task) == ("running", run_id, "builder:claim")
    saved = conn.execute(
        "SELECT canonical_payload,canonical_digest FROM gate_override_proposals "
        "WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()
    proposal = json.loads(saved["canonical_payload"])["proposal"]
    assert proposal["target"] == "task-parent"
    assert proposal["source"]["current_run_id"] == run_id
    assert proposal["destination"] == {"status": "done"}
    assert proposal["dependency_changes"][0]["action"] == "release"
    approval = conn.execute(
        "SELECT state,operation FROM write_gate_kanban_approvals WHERE request_id=?",
        (prepared["request_id"],),
    ).fetchone()
    assert tuple(approval) == ("prepared", "kanban_execute_task_gate_override")


def test_task_override_execution_schema_has_separate_task_identity_records(
    task_override_case,
):
    conn, _module, _initiative_id, _run_id = task_override_case

    record_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(task_gate_override_records)")
    }
    assert record_columns >= {
        "request_id",
        "approval_id",
        "mutation_id",
        "initiative_card_id",
        "initiative_id",
        "task_card_id",
        "task_id",
        "from_status",
        "to_status",
        "override_authority_ref",
        "override_reason",
        "unsatisfied_gates",
        "movement_basis",
        "result",
        "actor_evidence",
        "canonical_payload",
        "created_at",
    }
    decision_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(task_active_decisions)")
    }
    assert decision_columns >= {
        "decision_id",
        "task_card_id",
        "task_id",
        "initiative_id",
        "decision_kind",
        "authority_ref",
        "canonical_payload",
        "active",
        "created_at",
    }

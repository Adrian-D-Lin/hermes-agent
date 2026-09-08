"""Immutable override preparation metadata and single Write-Gate authority."""

import importlib
import json
import sqlite3
import hashlib

import pytest

from tests.test_adrian_kanban_s3_initiatives import commands_module  # noqa: F401
from tests.test_adrian_kanban_s3_override_approval import evidence
from writegate.kanban_approvals import create_kanban_approval_schema


@pytest.fixture
def proposal_case(commands_module):
    schema = importlib.import_module(f"{commands_module.__package__}.schema")
    proposals = importlib.import_module(
        f"{commands_module.__package__}.override_proposals"
    )
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    schema.create_schema(conn)
    create_kanban_approval_schema(conn)
    conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
    conn.execute("""INSERT INTO adrian_kanban_cards
        (card_type,initiative_id,title,created_at,board_slug)
        VALUES ('initiative','initiative-1','Initiative',1000,'orchestrator')""")
    yield conn, proposals
    conn.close()


def arguments(**changes):
    args = dict(
        request_id="override-execution-1",
        approval_id="override-approval-1",
        initial_authorizer=evidence("human-message-1"),
        initial_session_id="human-session",
        initial_message_id="human-message-1",
        initial_quote="Move initiative-1 to D3, preserving the unmet review.\n",
        executor_session_id="agent-session",
        executor_profile="default",
        proposal={
            "operation": "kanban_execute_initiative_gate_override",
            "initiative_id": "initiative-1",
            "target": "initiative-1",
            "target_kind": "initiative",
            "board": "orchestrator",
            "expected_version": 0,
            "source": {"phase": "D1", "segment_id": None, "predecessor_id": 1},
            "destination": {"phase": "D3", "segment_id": None},
            "gates": [{"code": "phase_close", "result": "unmet"}],
            "non_bypassable_checks": [{"code": "reconciliation", "result": "met"}],
            "claim_run_closures": [],
            "dependency_changes": [],
            "downstream_exceptions": [],
            "reconciliation_ref": "reconciliation-1",
            "override_reason": "Adrian explicitly directed this movement",
            "commentary": "Gate criteria overridden, not satisfied.",
        },
        now=1010,
        expires_at=1200,
    )
    args.update(changes)
    return args


def record(conn, proposals, args):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        return proposals.store_prepared_override(conn, **args)


def test_preparation_preserves_unmet_gates_without_moving_or_approving(proposal_case):
    conn, proposals = proposal_case
    before = tuple(conn.execute("SELECT * FROM adrian_kanban_cards").fetchone())
    result = record(conn, proposals, arguments())
    assert tuple(conn.execute("SELECT * FROM adrian_kanban_cards").fetchone()) == before
    assert (
        conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 0
    )
    saved = conn.execute("SELECT * FROM gate_override_proposals").fetchone()
    approval = conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone()
    payload = json.loads(saved["canonical_payload"])
    assert payload["proposal"]["gates"][0]["result"] == "unmet"
    quote = arguments()["initial_quote"]
    assert payload["initial_instruction"]["quote"] == quote
    assert (
        payload["initial_instruction"]["quote_sha256"]
        == hashlib.sha256(quote.encode()).hexdigest()
    )
    assert (
        hashlib.sha256(saved["canonical_payload"].encode()).hexdigest()
        == saved["canonical_digest"]
    )
    assert result["request_id"] == approval["request_id"] == saved["request_id"]
    assert (
        result["canonical_digest"]
        == approval["canonical_digest"]
        == saved["canonical_digest"]
    )
    assert approval["state"] == "prepared"
    assert approval["requires_distinct_authorizer"] == 1
    assert approval["approved_at"] is None
    assert approval["operation"] == payload["proposal"]["operation"]


def test_same_initial_action_cannot_prepare_a_new_request_with_a_new_key(proposal_case):
    conn, proposals = proposal_case
    args = arguments()
    record(conn, proposals, args)
    args.update(request_id="different-request", approval_id="different-approval")
    with pytest.raises(ValueError):
        record(conn, proposals, args)
    assert (
        conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "gap",
    [
        "forged",
        "expired",
        "wrong_message",
        "blank_quote",
        "expired_proposal",
        "failed_non_bypassable",
        "wrong_board",
        "stale_version",
    ],
)
def test_invalid_preparation_leaves_no_proposal_or_approval(proposal_case, gap):
    conn, proposals = proposal_case
    args = arguments()
    if gap == "forged":
        args["initial_authorizer"] = {"request_id": "human-message-1"}
    elif gap == "expired":
        args["now"] = 1121
    elif gap == "wrong_message":
        args["initial_message_id"] = "different-message"
    elif gap == "blank_quote":
        args["initial_quote"] = " "
    elif gap == "expired_proposal":
        args["expires_at"] = args["now"]
    elif gap == "failed_non_bypassable":
        args["proposal"]["non_bypassable_checks"][0]["result"] = "unmet"
    elif gap == "wrong_board":
        args["proposal"]["board"] = "another-board"
    elif gap == "stale_version":
        args["proposal"]["expected_version"] = 1
    with pytest.raises(ValueError):
        record(conn, proposals, args)
    assert (
        conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 0
    )


def test_task_proposal_uses_task_version_but_keeps_its_parent_initiative(proposal_case):
    conn, proposals = proposal_case
    conn.execute("""INSERT INTO adrian_kanban_cards
        (card_type,initiative_id,task_id,title,created_at,board_slug,record_version)
        VALUES ('task','initiative-1','task-1','Task',1000,'orchestrator',2)""")
    args = arguments()
    args["proposal"].update(
        operation="kanban_execute_task_gate_override",
        target_kind="task",
        target="task-1",
        expected_version=2,
        source={"status": "review"},
        destination={"status": "done"},
    )
    record(conn, proposals, args)
    approval = conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone()
    assert approval["initiative_id"] == "initiative-1"
    assert approval["expected_version"] == 2
    assert approval["operation"] == "kanban_execute_task_gate_override"
    assert (
        conn.execute(
            "SELECT record_version FROM adrian_kanban_cards WHERE task_id='task-1'"
        ).fetchone()[0]
        == 2
    )


def test_preparation_requires_and_obeys_the_caller_transaction(proposal_case):
    conn, proposals = proposal_case
    args = arguments()
    with pytest.raises(ValueError):
        proposals.store_prepared_override(conn, **args)
    conn.execute("BEGIN IMMEDIATE")
    proposals.store_prepared_override(conn, **args)
    conn.rollback()
    assert (
        conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("collision", ["request_id", "approval_id"])
def test_new_event_cannot_reuse_another_proposals_identity(proposal_case, collision):
    conn, proposals = proposal_case
    record(conn, proposals, arguments())
    second = arguments(
        request_id="request-2",
        approval_id="approval-2",
        initial_authorizer=evidence("human-message-2"),
        initial_message_id="human-message-2",
    )
    second[collision] = arguments()[collision]
    with pytest.raises(ValueError):
        record(conn, proposals, second)
    assert (
        conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 1
    )


def test_task_under_closed_initiative_cannot_get_an_approvable_proposal(proposal_case):
    conn, proposals = proposal_case
    conn.execute("UPDATE adrian_kanban_cards SET closed_at=1009")
    conn.execute("""INSERT INTO adrian_kanban_cards
        (card_type,initiative_id,task_id,title,created_at,board_slug)
        VALUES ('task','initiative-1','task-1','Task',1000,'orchestrator')""")
    args = arguments()
    args["proposal"].update(
        operation="kanban_execute_task_gate_override",
        target_kind="task",
        target="task-1",
        source={"status": "review"},
        destination={"status": "done"},
    )
    with pytest.raises(ValueError):
        record(conn, proposals, args)
    assert (
        conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 0
    )


def test_writegate_insert_failure_rolls_back_the_new_proposal(proposal_case):
    from tests.test_adrian_kanban_s2 import _preparation
    from writegate.kanban_approvals import KanbanInitiativeApprovalHost

    conn, proposals = proposal_case
    host = KanbanInitiativeApprovalHost(evidence("unrelated-event"))
    host.prepare(
        conn,
        _preparation(approval_id="override-approval-1", request_id="unrelated-request"),
        now=1010,
    )
    before = dict(conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone())
    with pytest.raises(ValueError):
        record(conn, proposals, arguments())
    assert (
        conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
    )
    assert (
        dict(conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone())
        == before
    )


def test_closed_initiative_prepares_one_explicit_reopen_and_move(proposal_case):
    conn, proposals = proposal_case
    conn.execute("UPDATE adrian_kanban_cards SET closed_at=1009")
    args = arguments()
    args["proposal"]["source"]["closed_at"] = 1009
    args["proposal"]["destination"]["closed_at"] = None
    result = record(conn, proposals, args)
    saved = json.loads(result["canonical_payload"])["proposal"]
    assert saved["source"]["closed_at"] == 1009
    assert saved["destination"] == {"phase": "D3", "segment_id": None, "closed_at": None}
    assert conn.execute("SELECT closed_at FROM adrian_kanban_cards").fetchone()[0] == 1009
    assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 0
    approvals = conn.execute("SELECT state, canonical_digest FROM write_gate_kanban_approvals").fetchall()
    assert len(approvals) == 1
    assert tuple(approvals[0]) == ("prepared", result["canonical_digest"])


@pytest.mark.parametrize("case", ["missing_source", "missing_destination", "stale", "false", "string", "close", "source_shape", "destination_shape"])
def test_inexact_reopening_cannot_prepare_approval(proposal_case, case):
    conn, proposals = proposal_case
    conn.execute("UPDATE adrian_kanban_cards SET closed_at=1009")
    args = arguments()
    source, destination = args["proposal"]["source"], args["proposal"]["destination"]
    source["closed_at"] = 1009
    destination["closed_at"] = None
    if case == "missing_source":
        del source["closed_at"]
    elif case == "missing_destination":
        del destination["closed_at"]
    elif case == "stale":
        source["closed_at"] = 1008
    elif case == "false":
        source["closed_at"] = False
    elif case == "string":
        source["closed_at"] = "1009"
    elif case == "close":
        destination["closed_at"] = 1010
    elif case == "source_shape":
        args["proposal"]["source"] = []
    else:
        args["proposal"]["destination"] = None
    with pytest.raises(ValueError):
        record(conn, proposals, args)
    assert conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0] == 0
    assert conn.execute("SELECT closed_at FROM adrian_kanban_cards").fetchone()[0] == 1009


@pytest.mark.parametrize("field", ["source", "destination"])
def test_open_initiative_cannot_claim_or_propose_closure(proposal_case, field):
    conn, proposals = proposal_case
    args = arguments()
    args["proposal"][field]["closed_at"] = 1009
    with pytest.raises(ValueError):
        record(conn, proposals, args)
    assert conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["source", "destination"])
def test_task_override_cannot_carry_a_closure_change(proposal_case, field):
    conn, proposals = proposal_case
    conn.execute("""INSERT INTO adrian_kanban_cards
        (card_type,initiative_id,task_id,title,created_at,board_slug)
        VALUES ('task','initiative-1','task-1','Task',1000,'orchestrator')""")
    args = arguments()
    args["proposal"].update(operation="kanban_execute_task_gate_override", target_kind="task", target="task-1", source={"status": "review"}, destination={"status": "done"})
    args["proposal"][field]["closed_at"] = 1009
    with pytest.raises(ValueError):
        record(conn, proposals, args)
    assert conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0] == 0

"""Exact approved override snapshot consumption."""

import json

import pytest

from tests.test_adrian_kanban_s3_override_proposals import (  # noqa: F401
    arguments, commands_module, proposal_case, record,
)
from tests.test_adrian_kanban_s3_override_approval import evidence
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


def prepared(proposal_case):
    conn, module = proposal_case
    args = arguments()
    snapshot = record(conn, module, args)
    host = KanbanInitiativeApprovalHost(args["initial_authorizer"])
    host.approve_distinct(
        conn, args["approval_id"], evidence("approval-message-2"),
        expected_request_id=args["request_id"],
        expected_canonical_digest=snapshot["canonical_digest"],
        approval_quote="Approve this exact change.", now=1011,
    )
    return conn, module, args, snapshot


def consume(module, conn, args, **changes):
    fields = dict(
        request_id=args["request_id"], operation=args["proposal"]["operation"],
        target=args["proposal"]["target"], executor_session_id=args["executor_session_id"],
        executor_profile=args["executor_profile"], current_proposal=args["proposal"],
        mutation_id="mutation-1", idempotency_key="idempotency-1", now=1012,
    )
    fields.update(changes)
    return module.consume_revalidated_override(conn, **fields)


def test_exact_snapshot_consumes_once_and_preserves_approval_audit(proposal_case):
    conn, module, args, snapshot = prepared(proposal_case)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        receipt = consume(module, conn, args)
    assert receipt["canonical_payload"] == snapshot["canonical_payload"]
    assert receipt["canonical_digest"] == snapshot["canonical_digest"]
    assert receipt["approval_id"] == args["approval_id"]
    approval_evidence = json.loads(receipt["approval_evidence"])
    assert approval_evidence["second_authorizer"]["request_id"] == "approval-message-2"
    assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "consumed"
    assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 0
    with pytest.raises(ValueError), conn:
        conn.execute("BEGIN IMMEDIATE")
        consume(module, conn, args)


@pytest.mark.parametrize("field,value", [
    ("request_id", "wrong"), ("operation", "kanban_execute_task_gate_override"),
    ("target", "initiative-other"), ("executor_session_id", "other"),
    ("executor_profile", "builder-tester"), ("now", 1200),
    ("now", True), ("mutation_id", ""), ("idempotency_key", " "),
])
def test_wrong_execution_binding_does_not_consume(proposal_case, field, value):
    conn, module, args, _ = prepared(proposal_case)
    with pytest.raises(ValueError), conn:
        conn.execute("BEGIN IMMEDIATE")
        consume(module, conn, args, **{field: value})
    assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"


@pytest.mark.parametrize("field,value", [
    ("source", {"phase": "D2", "segment_id": None, "predecessor_id": 2}),
    ("destination", {"phase": "D4", "segment_id": None}),
    ("gates", [{"code": "phase_close", "result": "met"}]),
    ("claim_run_closures", ["new-run"]), ("dependency_changes", ["new-dependent"]),
    ("downstream_exceptions", ["new-exception"]), ("expected_version", 1),
    ("reconciliation_ref", "new-reconciliation"),
])
def test_changed_derived_input_requires_new_approval(proposal_case, field, value):
    conn, module, args, _ = prepared(proposal_case)
    current = dict(args["proposal"], **{field: value})
    with pytest.raises(ValueError), conn:
        conn.execute("BEGIN IMMEDIATE")
        consume(module, conn, args, current_proposal=current)
    assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"


@pytest.mark.parametrize("corruption", ["payload", "digest", "legacy", "receipt", "cancelled", "prepared"])
def test_corrupt_or_unapproved_snapshot_cannot_execute(proposal_case, corruption):
    conn, module, args, _ = prepared(proposal_case)
    if corruption == "payload":
        conn.execute("UPDATE gate_override_proposals SET canonical_payload='{}'")
    elif corruption == "digest":
        conn.execute("UPDATE gate_override_proposals SET canonical_digest='wrong'")
    elif corruption == "legacy":
        conn.execute("UPDATE write_gate_kanban_approvals SET requires_distinct_authorizer=0")
    elif corruption == "receipt":
        conn.execute("UPDATE write_gate_kanban_approvals SET approval_evidence='{}'")
    else:
        conn.execute("UPDATE write_gate_kanban_approvals SET state=?", (corruption,))
    before = dict(conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone())
    with pytest.raises(ValueError), conn:
        conn.execute("BEGIN IMMEDIATE")
        consume(module, conn, args)
    assert dict(conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone()) == before


@pytest.mark.parametrize("corruption", ["top_extra", "initial_extra", "executor_extra", "noncanonical"])
def test_stored_payload_requires_exact_shape_and_actual_canonical_bytes(
    proposal_case, corruption
):
    conn, module, args, _ = prepared(proposal_case)
    saved = conn.execute(
        "SELECT canonical_payload FROM gate_override_proposals"
    ).fetchone()[0]
    payload = json.loads(saved)
    if corruption == "top_extra":
        payload["unexpected"] = "ignored"
    elif corruption == "initial_extra":
        payload["initial_instruction"]["unexpected"] = "ignored"
    elif corruption == "executor_extra":
        payload["executor"]["unexpected"] = "ignored"
    if corruption == "noncanonical":
        corrupted = json.dumps(payload, indent=2)
    else:
        corrupted = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "UPDATE gate_override_proposals SET canonical_payload=?", (corrupted,)
    )
    with pytest.raises(ValueError), conn:
        conn.execute("BEGIN IMMEDIATE")
        consume(module, conn, args)
    assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"


def test_second_authorizer_receipt_requires_exact_host_evidence_shape(proposal_case):
    conn, module, args, _ = prepared(proposal_case)
    raw = conn.execute(
        "SELECT approval_evidence FROM write_gate_kanban_approvals"
    ).fetchone()[0]
    receipt = json.loads(raw)
    receipt["second_authorizer"]["unexpected"] = "ignored"
    conn.execute(
        "UPDATE write_gate_kanban_approvals SET approval_evidence=?",
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")),),
    )
    with pytest.raises(ValueError), conn:
        conn.execute("BEGIN IMMEDIATE")
        consume(module, conn, args)
    assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"


def test_later_mutation_failure_rolls_back_consumption(proposal_case):
    conn, module, args, _ = prepared(proposal_case)
    with pytest.raises(RuntimeError), conn:
        conn.execute("BEGIN IMMEDIATE")
        consume(module, conn, args)
        raise RuntimeError("movement failed")
    assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"


def test_consumption_requires_transaction(proposal_case):
    conn, module, args, _ = prepared(proposal_case)
    with pytest.raises(ValueError):
        consume(module, conn, args)

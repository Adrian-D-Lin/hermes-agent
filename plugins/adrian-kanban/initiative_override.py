"""Pure derivation of an initiative transition gate-override proposal."""

from __future__ import annotations

import json

from .initiative_mutations import (
    CommandRejected,
    INITIATIVE_PHASES,
    _validate_reconciliation,
    _validate_segment_projection,
)
from .override_proposals import consume_revalidated_override, store_prepared_override
from .transition_evidence import validate_transition_evidence


def derive_initiative_transition_override(
    conn,
    *,
    initiative_id,
    board,
    to_phase,
    to_segment_id,
    reconciliation_ref,
    override_reason,
    executor_session_id,
    executor_profile,
):
    """Derive a gate-override proposal from live SQLite state without writing."""
    if executor_profile != "default":
        raise ValueError(
            "initiative gate override requires the default orchestrator executor"
        )
    if not override_reason or not override_reason.strip():
        raise ValueError("override_reason is required")

    card = conn.execute(
        "SELECT id, closed_at, record_version "
        "FROM adrian_kanban_cards WHERE initiative_id = ?",
        (initiative_id,),
    ).fetchone()
    if card is None:
        raise ValueError(f"unknown initiative {initiative_id!r}")

    card_id = card["id"]
    source_closed_at = card["closed_at"]
    expected_version = card["record_version"]

    latest = conn.execute(
        "SELECT transition_id, to_phase, to_segment_id "
        "FROM initiative_transitions WHERE initiative_id = ? "
        "ORDER BY transition_id DESC LIMIT 1",
        (initiative_id,),
    ).fetchone()
    if latest is None:
        raise ValueError(f"unknown initiative {initiative_id!r}")

    source_phase = latest["to_phase"]
    source_segment_id = latest["to_segment_id"]
    source_predecessor_id = latest["transition_id"]

    if to_phase not in INITIATIVE_PHASES:
        raise ValueError(f"unknown target phase {to_phase!r}")
    if to_phase in {"DEV2", "DEV3", "DEV4"}:
        if not to_segment_id:
            raise ValueError("target segment is required for DEV2/DEV3/DEV4")
    elif to_segment_id is not None:
        raise ValueError("target segment must be absent for non-DEV phases")

    try:
        _validate_reconciliation(
            conn,
            card_id,
            initiative_id,
            board,
            reconciliation_ref,
            source_predecessor_id,
            source_phase,
            source_segment_id,
            to_phase,
            to_segment_id,
        )
    except CommandRejected:
        raise ValueError("reconciliation validation failed") from None
    if to_phase in {"DEV2", "DEV3", "DEV4"}:
        _validate_segment_projection(conn, card_id, to_segment_id)

    candidates = conn.execute(
        "SELECT result_id FROM initiative_phase_results "
        "WHERE initiative_card_id = ? AND result_kind = 'phase_close' AND accepted = 1 "
        "ORDER BY created_at DESC, result_id DESC",
        (card_id,),
    ).fetchall()
    met_ref = None
    for candidate in candidates:
        try:
            validate_transition_evidence(
                conn,
                initiative_card_id=card_id,
                initiative_id=initiative_id,
                from_phase=source_phase,
                from_segment_id=source_segment_id,
                to_phase=to_phase,
                to_segment_id=to_segment_id,
                previous_transition_id=source_predecessor_id,
                phase_close_ref=candidate["result_id"],
            )
        except ValueError:
            continue
        met_ref = candidate["result_id"]
        break
    if met_ref is None:
        gate_entry = {
            "code": "phase_close",
            "result": "unmet",
            "evidence_ref": None,
            "observed": "no accepted phase_close authorizes this exact transition",
        }
    else:
        gate_entry = {
            "code": "phase_close",
            "result": "met",
            "evidence_ref": met_ref,
            "observed": "accepted phase_close authorizes this exact transition",
        }

    non_bypassable_checks = [
        {
            "code": "reconciliation",
            "result": "met",
            "evidence_ref": reconciliation_ref,
            "observed": "mandatory reconciliation validator passed",
        },
        {
            "code": "segment",
            "result": "met",
            "evidence_ref": to_segment_id,
            "observed": "mandatory segment validator passed",
        },
    ]

    commentary = (
        f"Per Adrian's explicit instruction, {initiative_id} moved from "
        f"{source_phase} to {to_phase}; gate criteria overridden, not satisfied."
    )

    return {
        "operation": "kanban_execute_initiative_gate_override",
        "initiative_id": initiative_id,
        "target": initiative_id,
        "target_kind": "initiative",
        "board": board,
        "expected_version": expected_version,
        "source": {
            "phase": source_phase,
            "segment_id": source_segment_id,
            "predecessor_id": source_predecessor_id,
            "closed_at": source_closed_at,
        },
        "destination": {
            "phase": to_phase,
            "segment_id": to_segment_id,
            "closed_at": None,
        },
        "gates": [gate_entry],
        "non_bypassable_checks": non_bypassable_checks,
        "claim_run_closures": [],
        "dependency_changes": [],
        "downstream_exceptions": [],
        "reconciliation_ref": reconciliation_ref,
        "override_reason": override_reason,
        "commentary": commentary,
    }


def prepare_initiative_transition_override(
    conn,
    *,
    initial_authorizer,
    initial_session_id,
    initial_message_id,
    initial_quote,
    executor_session_id,
    executor_profile,
    initiative_id,
    board,
    to_phase,
    to_segment_id,
    reconciliation_ref,
    override_reason,
    now,
    expires_at,
):
    """Prepare one initiative transition override on the caller's open transaction.

    Thin coordinator: derives the proposal from live state, then stores it and
    prepares the Write-Gate approval via ``store_prepared_override``. Never
    begins, commits, or rolls back; never mints evidence, displays UI, approves,
    consumes, or mutates an initiative.
    """
    if initial_session_id != executor_session_id:
        raise ValueError("initial and executor sessions must be identical")

    request_id = f"kanban-gate-override:{initial_message_id}"
    approval_id = f"kanban-gate-override-approval:{initial_message_id}"

    proposal = derive_initiative_transition_override(
        conn,
        initiative_id=initiative_id,
        board=board,
        to_phase=to_phase,
        to_segment_id=to_segment_id,
        reconciliation_ref=reconciliation_ref,
        override_reason=override_reason,
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
    )

    return store_prepared_override(
        conn,
        request_id=request_id,
        approval_id=approval_id,
        initial_authorizer=initial_authorizer,
        initial_session_id=initial_session_id,
        initial_message_id=initial_message_id,
        initial_quote=initial_quote,
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
        proposal=proposal,
        now=now,
        expires_at=expires_at,
    )


def execute_approved_initiative_transition_override(
    conn,
    *,
    request_id,
    executor_session_id,
    executor_profile,
    mutation_id,
    idempotency_key,
    now,
):
    """Atomically execute an approved initiative gate override on the caller's open transaction.

    Never begins, commits, or rolls back; a late failure propagates so the
    caller's rollback reverses approval consumption, version/reopen, transition,
    and records.
    """
    if not request_id or not isinstance(request_id, str):
        raise ValueError("request_id is required")
    if not executor_session_id or not isinstance(executor_session_id, str):
        raise ValueError("executor_session_id is required")
    if executor_profile != "default":
        raise ValueError(
            "initiative gate override requires the default orchestrator executor"
        )
    if not mutation_id or not isinstance(mutation_id, str):
        raise ValueError("mutation_id is required")
    if not idempotency_key or not isinstance(idempotency_key, str):
        raise ValueError("idempotency_key is required")
    if not isinstance(now, int) or now <= 0:
        raise ValueError("now must be a positive integer")

    row = conn.execute(
        "SELECT canonical_payload FROM gate_override_proposals WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown override request {request_id!r}")
    payload = json.loads(row["canonical_payload"])
    stored_proposal = payload["proposal"]
    executor = payload["executor"]
    if executor.get("session_id") != executor_session_id:
        raise ValueError("executor session does not match the approved override")
    if executor.get("profile") != executor_profile:
        raise ValueError("executor profile does not match the approved override")

    rederived = derive_initiative_transition_override(
        conn,
        initiative_id=stored_proposal["initiative_id"],
        board=stored_proposal["board"],
        to_phase=stored_proposal["destination"]["phase"],
        to_segment_id=stored_proposal["destination"]["segment_id"],
        reconciliation_ref=stored_proposal["reconciliation_ref"],
        override_reason=stored_proposal["override_reason"],
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
    )
    if rederived != stored_proposal:
        raise ValueError(
            "initiative record version no longer matches the approved override"
        )

    consumed = consume_revalidated_override(
        conn,
        request_id=request_id,
        operation=stored_proposal["operation"],
        target=stored_proposal["initiative_id"],
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
        current_proposal=rederived,
        mutation_id=mutation_id,
        idempotency_key=idempotency_key,
        now=now,
    )

    card = conn.execute(
        "SELECT id, record_version FROM adrian_kanban_cards WHERE initiative_id = ?",
        (stored_proposal["initiative_id"],),
    ).fetchone()
    if card is None:
        raise ValueError(f"unknown initiative {stored_proposal['initiative_id']!r}")
    card_id = card["id"]
    expected_version = stored_proposal["expected_version"]
    updated = conn.execute(
        "UPDATE adrian_kanban_cards SET record_version = ?, closed_at = NULL "
        "WHERE id = ? AND record_version = ?",
        (expected_version + 1, card_id, expected_version),
    )
    if updated.rowcount != 1:
        raise ValueError("initiative record version no longer matches the approved override")

    unsatisfied_gates = [gate for gate in rederived["gates"] if gate["result"] != "met"]
    canonical_payload = json.dumps(
        {
            **rederived,
            "movement_basis": "adrian_gate_override",
            "result": "accepted_with_active_decision",
            "override_request_id": request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    actor_evidence = json.dumps(
        {
            "executor_session_id": executor_session_id,
            "executor_profile": executor_profile,
            "approval_evidence": consumed["approval_evidence"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    transition_id = rederived["source"]["predecessor_id"] + 1
    conn.execute(
        "INSERT INTO initiative_transitions ("
        "transition_id, initiative_id, initiative_card_id, previous_transition_id, "
        "from_phase, to_phase, from_segment_id, to_segment_id, trigger, "
        "canonical_payload, actor_evidence, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            transition_id,
            stored_proposal["initiative_id"],
            card_id,
            rederived["source"]["predecessor_id"],
            rederived["source"]["phase"],
            rederived["destination"]["phase"],
            rederived["source"]["segment_id"],
            rederived["destination"]["segment_id"],
            "adrian_instruction",
            canonical_payload,
            actor_evidence,
            now,
        ),
    )

    conn.execute(
        "INSERT INTO gate_override_records ("
        "request_id, approval_id, mutation_id, initiative_card_id, initiative_id, "
        "from_phase, from_segment_id, to_phase, to_segment_id, "
        "override_authority_ref, override_reason, unsatisfied_gates, movement_basis, "
        "result, actor_evidence, canonical_payload, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request_id,
            consumed["approval_id"],
            mutation_id,
            card_id,
            stored_proposal["initiative_id"],
            rederived["source"]["phase"],
            rederived["source"]["segment_id"],
            rederived["destination"]["phase"],
            rederived["destination"]["segment_id"],
            request_id,
            rederived["override_reason"],
            json.dumps(unsatisfied_gates, sort_keys=True, separators=(",", ":")),
            "adrian_gate_override",
            "accepted_with_active_decision",
            actor_evidence,
            canonical_payload,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO initiative_active_decisions ("
        "decision_id, initiative_card_id, initiative_id, decision_kind, "
        "authority_ref, canonical_payload, active, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (
            f"decision:{request_id}",
            card_id,
            stored_proposal["initiative_id"],
            "adrian_gate_override",
            request_id,
            canonical_payload,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO initiative_generated_comments ("
        "initiative_card_id, initiative_id, source_kind, source_ref, body, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (
            card_id,
            stored_proposal["initiative_id"],
            "adrian_gate_override",
            request_id,
            rederived["commentary"],
            now,
        ),
    )

    return {
        "initiative_id": stored_proposal["initiative_id"],
        "from_phase": rederived["source"]["phase"],
        "to_phase": rederived["destination"]["phase"],
        "transition_id": transition_id,
        "record_version": expected_version + 1,
        "request_id": request_id,
        "result": "accepted_with_active_decision",
    }

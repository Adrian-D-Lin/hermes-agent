"""Pure derivation of an initiative transition gate-override proposal."""

from __future__ import annotations

from .initiative_mutations import (
    CommandRejected,
    INITIATIVE_PHASES,
    _validate_reconciliation,
    _validate_segment_projection,
)
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
            phase_close_ref=reconciliation_ref,
        )
    except ValueError:
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
            "evidence_ref": reconciliation_ref,
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

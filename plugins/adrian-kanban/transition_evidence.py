"""Admission-only ordinary initiative transition evidence for Adrian Kanban.

Implements ratified v0.28 section 7.12 step 5: validates that an accepted
``phase_close`` result in ``initiative_phase_results`` authorizes the exact
requested transition of this initiative, without performing any mutation.
"""

from __future__ import annotations

import json
from typing import Any

from .initiative_checkpoints import _resolve_task_ref

# Phases with implemented admission validators; others fail explicitly.
_SUPPORTED_PHASES = frozenset({"D1", "D2", "D3", "D4"})


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool) and value > 0


def _is_nonblank_str(value: Any) -> bool:
    return type(value) is str and value.strip() != ""


def validate_transition_evidence(
    conn: Any,
    *,
    initiative_card_id: int,
    initiative_id: str,
    from_phase: str,
    from_segment_id: str | None,
    to_phase: str,
    to_segment_id: str | None,
    previous_transition_id: int,
    phase_close_ref: str,
) -> dict[str, Any]:
    """Validate the accepted close result that authorizes an ordinary transition.

    Read-only: no writes, approvals, network, clock, or artifact re-download.
    Returns the decoded accepted close result dict on success; raises
    ``ValueError`` otherwise.
    """
    # Only the departing phase's admitted close is cited here; its admission
    # validator must be implemented. The destination is proven solely by the
    # close's own next_route, so it is not gated on implemented validators.
    if from_phase not in _SUPPORTED_PHASES:
        raise ValueError(
            f"no admission validator implemented for departing phase {from_phase!r}; "
            "ordinary transitions currently require D1, D2, D3, or D4"
        )
    if from_segment_id is not None:
        raise ValueError("source segment must be null for D1-D4")
    if to_segment_id is not None:
        raise ValueError("destination segment must be null for D1-D4")
    if not _is_nonblank_str(initiative_id):
        raise ValueError("initiative_id must be a nonblank string")
    if not _is_nonblank_str(phase_close_ref):
        raise ValueError("phase_close_ref must be a nonblank string")
    if not _is_positive_int(previous_transition_id):
        raise ValueError("previous_transition_id must be a positive integer")

    # Latest authoritative position must exactly match the requested departure.
    latest = conn.execute(
        "SELECT transition_id, to_phase, to_segment_id "
        "FROM initiative_transitions WHERE initiative_id = ? "
        "ORDER BY transition_id DESC LIMIT 1",
        (initiative_id,),
    ).fetchone()
    if latest is None:
        raise ValueError("no initiative transition found")
    if latest["transition_id"] != previous_transition_id:
        raise ValueError(
            "latest transition_id does not match previous_transition_id"
        )
    if latest["to_phase"] != from_phase:
        raise ValueError("latest transition phase does not match from_phase")
    if latest["to_segment_id"] != from_segment_id:
        raise ValueError("latest transition segment does not match from_segment_id")

    expected_contract = f"adrian-kanban.lifecycle.{from_phase.lower()}"
    row = conn.execute(
        """
        SELECT canonical_payload, accepted_task_refs, accepted_checkpoint_refs, actor_evidence
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = ?
          AND segment_id IS NULL
          AND result_kind = 'phase_close'
          AND accepted = 1
          AND contract_id = ?
          AND contract_version = '1'
        """,
        (
            phase_close_ref,
            initiative_card_id,
            initiative_id,
            from_phase,
            expected_contract,
        ),
    ).fetchone()
    if row is None:
        raise ValueError(
            "phase_close_ref does not resolve to an accepted phase_close for this "
            "initiative/card, departing phase/segment, and lifecycle contract"
        )

    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("close canonical_payload is malformed JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("close canonical_payload must be an object")
    if payload.get("next_route") != to_phase:
        raise ValueError("close next_route does not match the requested to_phase")

    try:
        actor = json.loads(row["actor_evidence"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("close actor_evidence is malformed JSON") from None
    if not isinstance(actor, dict):
        raise ValueError("close actor_evidence must be an object")
    source_transition_id = actor.get("source_transition_id")
    if not _is_positive_int(source_transition_id):
        raise ValueError(
            "close actor_evidence.source_transition_id must be a positive integer"
        )
    if source_transition_id != previous_transition_id:
        raise ValueError(
            "close actor_evidence.source_transition_id does not match the current "
            "predecessor transition"
        )

    try:
        task_refs = json.loads(row["accepted_task_refs"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("close accepted_task_refs is malformed JSON") from None
    if not isinstance(task_refs, list):
        raise ValueError("close accepted_task_refs must be a list")
    for ref in task_refs:
        if not _is_nonblank_str(ref):
            raise ValueError("close accepted_task_refs must contain nonblank strings")
    if len(task_refs) != len(set(task_refs)):
        raise ValueError("close accepted_task_refs must be unique")

    try:
        checkpoint_refs = json.loads(row["accepted_checkpoint_refs"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("close accepted_checkpoint_refs is malformed JSON") from None
    if not isinstance(checkpoint_refs, list):
        raise ValueError("close accepted_checkpoint_refs must be a list")
    for ref in checkpoint_refs:
        if not _is_nonblank_str(ref):
            raise ValueError(
                "close accepted_checkpoint_refs must contain nonblank strings"
            )
    if len(checkpoint_refs) != len(set(checkpoint_refs)):
        raise ValueError("close accepted_checkpoint_refs must be unique")

    for ref in task_refs:
        step, _, _ = _resolve_task_ref(conn, ref, initiative_card_id, initiative_id)
        if step.split(".")[0] != from_phase:
            raise ValueError(
                "accepted task ref does not belong to the departing phase"
            )

    for ref in checkpoint_refs:
        cp_row = conn.execute(
            """
            SELECT 1
            FROM initiative_phase_results
            WHERE result_id = ?
              AND initiative_card_id = ?
              AND initiative_id = ?
              AND phase = ?
              AND segment_id IS NULL
              AND result_kind = 'orchestration_checkpoint'
              AND accepted = 1
              AND contract_id = ?
              AND contract_version = '1'
            """,
            (ref, initiative_card_id, initiative_id, from_phase, expected_contract),
        ).fetchone()
        if cp_row is None:
            raise ValueError(
                "accepted checkpoint ref does not resolve to an accepted "
                "orchestration checkpoint for this initiative/card, phase, and contract"
            )

    return payload

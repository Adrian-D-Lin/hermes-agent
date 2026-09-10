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


def _validate_current_position(
    conn: Any,
    *,
    initiative_id: str,
    from_phase: str,
    from_segment_id: str | None,
    previous_transition_id: int,
) -> Any:
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
    return latest


def _validate_dev1_projection_transition(
    conn: Any,
    *,
    latest: Any,
    initiative_card_id: int,
    initiative_id: str,
    to_phase: str,
    to_segment_id: str | None,
    result_ref: str,
) -> dict[str, Any]:
    """Validate the accepted DEV1.7 projection that exits DEV1 into a segment."""
    if not _is_nonblank_str(to_segment_id):
        raise ValueError("DEV1 exit destination segment must be a nonblank string")
    row = conn.execute(
        """
        SELECT canonical_payload, actor_evidence
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV1'
          AND segment_id IS NULL
          AND result_kind = 'segment_manifest_projection'
          AND accepted = 1
          AND contract_id = 'adrian-kanban.lifecycle.dev1'
          AND contract_version = '1'
        """,
        (result_ref, initiative_card_id, initiative_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "phase_close_ref does not resolve to an accepted DEV1.7 "
            "segment_manifest_projection for this initiative/card and lifecycle contract"
        )

    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("projection canonical_payload is malformed JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("projection canonical_payload must be an object")

    try:
        actor = json.loads(row["actor_evidence"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("projection actor_evidence is malformed JSON") from None
    if not isinstance(actor, dict):
        raise ValueError("projection actor_evidence must be an object")
    source_transition_id = actor.get("source_transition_id")
    if not _is_positive_int(source_transition_id):
        raise ValueError(
            "projection actor_evidence.source_transition_id must be a positive integer"
        )
    if source_transition_id != latest["transition_id"]:
        raise ValueError(
            "projection actor_evidence.source_transition_id does not match the current "
            "predecessor transition"
        )

    projection_row = conn.execute(
        """
        SELECT projection_id, projection_version, parsed_segment_definitions, readiness_refs
        FROM initiative_segment_projections
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND validation_result = 'accepted'
        ORDER BY projection_version DESC LIMIT 1
        """,
        (initiative_card_id, initiative_id),
    ).fetchone()
    if projection_row is None:
        raise ValueError("no accepted segment projection found for this initiative/card")
    if payload.get("projection_id") != projection_row["projection_id"]:
        raise ValueError("payload projection_id does not match the latest accepted projection")
    if payload.get("projection_version") != projection_row["projection_version"]:
        raise ValueError(
            "payload projection_version does not match the latest accepted projection"
        )

    try:
        definitions = json.loads(projection_row["parsed_segment_definitions"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("projection parsed_segment_definitions is malformed JSON") from None
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("projection parsed_segment_definitions must be a nonempty list")
    segment_ids = [
        definition.get("segment_id") for definition in definitions
        if isinstance(definition, dict)
    ]
    if len(segment_ids) != len(definitions) or any(
        not _is_nonblank_str(segment_id) for segment_id in segment_ids
    ):
        raise ValueError("projection segments must each have a nonblank segment_id")
    first_segment_id = min(
        (
            (definition["ordinal"], definition["segment_id"])
            for definition in definitions
        ),
        key=lambda item: item[0],
    )[1]
    if to_phase != "DEV2":
        raise ValueError("DEV1 exit destination phase must be DEV2")
    if to_segment_id != first_segment_id:
        raise ValueError(
            "DEV1 exit destination segment must be the first segment by ordinal"
        )

    try:
        stored_readiness = json.loads(projection_row["readiness_refs"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("projection readiness_refs is malformed JSON") from None
    if not isinstance(stored_readiness, dict):
        raise ValueError("projection readiness_refs must parse to a dict")
    payload_readiness = payload.get("readiness_refs")
    if not isinstance(payload_readiness, dict):
        raise ValueError("payload readiness_refs must be an object")
    if payload_readiness != stored_readiness:
        raise ValueError("payload readiness_refs do not match the latest accepted projection")
    if set(payload_readiness) != set(segment_ids):
        raise ValueError("readiness_refs keys must equal projected segment IDs")
    refs = [value for value in payload_readiness.values()]
    if any(not _is_nonblank_str(ref) for ref in refs):
        raise ValueError("readiness_refs values must be nonblank strings")
    if len(set(refs)) != len(refs):
        raise ValueError("readiness_refs values must be unique")

    return payload


def _validate_dev2_checkpoint_transition(
    conn: Any,
    *,
    latest: Any,
    initiative_card_id: int,
    initiative_id: str,
    to_phase: str,
    to_segment_id: str | None,
    result_ref: str,
) -> dict[str, Any]:
    """Validate the accepted DEV2.3 orchestration checkpoint exiting DEV2."""
    row = conn.execute(
        """
        SELECT canonical_payload, actor_evidence
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV2'
          AND segment_id = ?
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = 'adrian-kanban.lifecycle.dev2'
          AND contract_version = '1'
        """,
        (result_ref, initiative_card_id, initiative_id, to_segment_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "phase_close_ref does not resolve to an accepted DEV2.3 "
            "orchestration checkpoint for this exact card/initiative/segment "
            "and lifecycle contract"
        )

    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("checkpoint canonical_payload is malformed JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("checkpoint canonical_payload must be an object")
    if payload.get("step") != "DEV2.3":
        raise ValueError("DEV2 exit checkpoint payload step must be DEV2.3")
    if payload.get("next_route") != to_phase:
        raise ValueError("DEV2 exit checkpoint next_route does not match to_phase")

    try:
        actor = json.loads(row["actor_evidence"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("checkpoint actor_evidence is malformed JSON") from None
    if not isinstance(actor, dict):
        raise ValueError("checkpoint actor_evidence must be an object")
    source_transition_id = actor.get("source_transition_id")
    if not _is_positive_int(source_transition_id):
        raise ValueError(
            "checkpoint actor_evidence.source_transition_id must be a positive integer"
        )
    if source_transition_id != latest["transition_id"]:
        raise ValueError(
            "checkpoint actor_evidence.source_transition_id does not match the current "
            "predecessor transition"
        )

    return payload


def _validate_dev3_checkpoint_transition(
    conn: Any,
    *,
    latest: Any,
    initiative_card_id: int,
    initiative_id: str,
    to_phase: str,
    to_segment_id: str | None,
    result_ref: str,
) -> dict[str, Any]:
    """Validate the accepted DEV3.9 orchestration checkpoint exiting DEV3."""
    row = conn.execute(
        """
        SELECT canonical_payload, actor_evidence
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV3'
          AND segment_id = ?
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = 'adrian-kanban.lifecycle.dev3'
          AND contract_version = '1'
        """,
        (result_ref, initiative_card_id, initiative_id, to_segment_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "phase_close_ref does not resolve to an accepted DEV3.9 "
            "orchestration checkpoint for this exact card/initiative/segment "
            "and lifecycle contract"
        )

    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("checkpoint canonical_payload is malformed JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("checkpoint canonical_payload must be an object")
    if payload.get("step") != "DEV3.9":
        raise ValueError("DEV3 exit checkpoint payload step must be DEV3.9")
    if payload.get("next_route") != to_phase:
        raise ValueError("DEV3 exit checkpoint next_route does not match to_phase")

    try:
        actor = json.loads(row["actor_evidence"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("checkpoint actor_evidence is malformed JSON") from None
    if not isinstance(actor, dict):
        raise ValueError("checkpoint actor_evidence must be an object")
    source_transition_id = actor.get("source_transition_id")
    if not _is_positive_int(source_transition_id):
        raise ValueError(
            "checkpoint actor_evidence.source_transition_id must be a positive integer"
        )
    if source_transition_id != latest["transition_id"]:
        raise ValueError(
            "checkpoint actor_evidence.source_transition_id does not match the current "
            "predecessor transition"
        )

    return payload


def _validate_dev4_checkpoint_transition(
    conn: Any,
    *,
    latest: Any,
    initiative_card_id: int,
    initiative_id: str,
    from_segment_id: str,
    to_phase: str,
    to_segment_id: str | None,
    result_ref: str,
) -> dict[str, Any]:
    """Validate the accepted DEV4.5 orchestration checkpoint exiting DEV4."""
    row = conn.execute(
        """
        SELECT canonical_payload, actor_evidence
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV4'
          AND segment_id = ?
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = 'adrian-kanban.lifecycle.dev4'
          AND contract_version = '1'
        """,
        (result_ref, initiative_card_id, initiative_id, from_segment_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "phase_close_ref does not resolve to an accepted DEV4.5 "
            "orchestration checkpoint for this exact card/initiative/segment "
            "and lifecycle contract"
        )

    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("checkpoint canonical_payload is malformed JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("checkpoint canonical_payload must be an object")
    if payload.get("step") != "DEV4.5":
        raise ValueError("DEV4 exit checkpoint payload step must be DEV4.5")
    if payload.get("action") != "admit_next_segment":
        raise ValueError("DEV4 exit checkpoint action must be admit_next_segment")
    if payload.get("completed_segment_id") != from_segment_id:
        raise ValueError(
            "DEV4 exit checkpoint completed_segment_id does not match the source segment"
        )
    if payload.get("next_route") != to_phase:
        raise ValueError("DEV4 exit checkpoint next_route does not match to_phase")
    if payload.get("next_segment_id") != to_segment_id:
        raise ValueError(
            "DEV4 exit checkpoint next_segment_id does not match the destination segment"
        )

    try:
        actor = json.loads(row["actor_evidence"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("checkpoint actor_evidence is malformed JSON") from None
    if not isinstance(actor, dict):
        raise ValueError("checkpoint actor_evidence must be an object")
    source_transition_id = actor.get("source_transition_id")
    if not _is_positive_int(source_transition_id):
        raise ValueError(
            "checkpoint actor_evidence.source_transition_id must be a positive integer"
        )
    if source_transition_id != latest["transition_id"]:
        raise ValueError(
            "checkpoint actor_evidence.source_transition_id does not match the current "
            "predecessor transition"
        )

    return payload


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
    if from_phase == "DEV1":
        if from_segment_id is not None:
            raise ValueError("source segment must be null for DEV1")
    elif from_phase == "DEV2":
        if not _is_nonblank_str(from_segment_id):
            raise ValueError("DEV2 source segment must be a nonblank string")
        if to_phase != "DEV3":
            raise ValueError("DEV2 destination phase must be DEV3")
        if to_segment_id != from_segment_id:
            raise ValueError("DEV2 destination segment must equal the source segment")
    elif from_phase == "DEV3":
        if not _is_nonblank_str(from_segment_id):
            raise ValueError("DEV3 source segment must be a nonblank string")
        if to_phase != "DEV4":
            raise ValueError("DEV3 destination phase must be DEV4")
        if to_segment_id != from_segment_id:
            raise ValueError("DEV3 destination segment must equal the source segment")
    elif from_phase == "DEV4":
        if not _is_nonblank_str(from_segment_id):
            raise ValueError("DEV4 source segment must be a nonblank string")
        if to_phase != "DEV2":
            raise ValueError("DEV4 destination phase must be DEV2")
        if not _is_nonblank_str(to_segment_id):
            raise ValueError("DEV4 destination segment must be a nonblank string")
    elif from_phase not in _SUPPORTED_PHASES:
        raise ValueError(
            f"no admission validator implemented for departing phase {from_phase!r}; "
            "ordinary transitions currently require D1, D2, D3, or D4"
        )
    else:
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
    latest = _validate_current_position(
        conn,
        initiative_id=initiative_id,
        from_phase=from_phase,
        from_segment_id=from_segment_id,
        previous_transition_id=previous_transition_id,
    )

    if from_phase == "DEV1":
        return _validate_dev1_projection_transition(
            conn,
            latest=latest,
            initiative_card_id=initiative_card_id,
            initiative_id=initiative_id,
            to_phase=to_phase,
            to_segment_id=to_segment_id,
            result_ref=phase_close_ref,
        )

    if from_phase == "DEV2":
        return _validate_dev2_checkpoint_transition(
            conn,
            latest=latest,
            initiative_card_id=initiative_card_id,
            initiative_id=initiative_id,
            to_phase=to_phase,
            to_segment_id=to_segment_id,
            result_ref=phase_close_ref,
        )

    if from_phase == "DEV3":
        return _validate_dev3_checkpoint_transition(
            conn,
            latest=latest,
            initiative_card_id=initiative_card_id,
            initiative_id=initiative_id,
            to_phase=to_phase,
            to_segment_id=to_segment_id,
            result_ref=phase_close_ref,
        )

    if from_phase == "DEV4":
        return _validate_dev4_checkpoint_transition(
            conn,
            latest=latest,
            initiative_card_id=initiative_card_id,
            initiative_id=initiative_id,
            from_segment_id=from_segment_id,
            to_phase=to_phase,
            to_segment_id=to_segment_id,
            result_ref=phase_close_ref,
        )

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

"""Admission-only orchestration checkpoint validator for Adrian Kanban.

This module validates that an orchestration checkpoint submission is structurally
and semantically valid for admission. It does NOT perform mutations, consume
approvals, advance versions, or write to the database. The existing initiative
handler owns all mutation and approval consumption logic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_D4_STEPS = frozenset({"D4.3", "D4.4"})
_DEV1_STEPS = frozenset({"DEV1.2", "DEV1.3"})
_RECOGNIZED_STEPS = _D4_STEPS | _DEV1_STEPS

_D4_CONTRACT_ID = "adrian-kanban.lifecycle.d4"
_DEV1_CONTRACT_ID = "adrian-kanban.lifecycle.dev1"
_CONTRACT_VERSION = "1"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CheckpointAdmission:
    result_id: str
    step: str
    phase: str
    iteration: int
    contract_id: str
    contract_version: str
    result: dict[str, Any]
    accepted_task_refs: list[str]
    accepted_checkpoint_refs: list[str]


def _is_nonblank_str(value: Any) -> bool:
    return type(value) is str and value.strip() != ""


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool) and value > 0


def _validate_update_shape(update: dict[str, Any]) -> None:
    required = {
        "result_id",
        "phase",
        "segment_id",
        "iteration",
        "contract_id",
        "contract_version",
        "result",
        "accepted_task_refs",
        "accepted_checkpoint_refs",
    }
    if set(update.keys()) != required:
        raise ValueError("update must contain exact required fields")

    if not _is_nonblank_str(update.get("result_id")):
        raise ValueError("result_id must be a nonblank string")
    if not _is_nonblank_str(update.get("phase")):
        raise ValueError("phase must be a nonblank string")
    if update.get("segment_id") is not None:
        raise ValueError("segment_id must be null")
    if not _is_positive_int(update.get("iteration")):
        raise ValueError("iteration must be a positive integer")
    if not _is_nonblank_str(update.get("contract_id")):
        raise ValueError("contract_id must be a nonblank string")
    if not _is_nonblank_str(update.get("contract_version")):
        raise ValueError("contract_version must be a nonblank string")
    if not isinstance(update.get("result"), dict):
        raise ValueError("result must be an object")
    if not isinstance(update.get("accepted_task_refs"), list):
        raise ValueError("accepted_task_refs must be a list")
    if not isinstance(update.get("accepted_checkpoint_refs"), list):
        raise ValueError("accepted_checkpoint_refs must be a list")

    for ref in update["accepted_task_refs"]:
        if not _is_nonblank_str(ref):
            raise ValueError("accepted_task_refs must contain nonblank strings")
    for ref in update["accepted_checkpoint_refs"]:
        if not _is_nonblank_str(ref):
            raise ValueError("accepted_checkpoint_refs must contain nonblank strings")

    if len(update["accepted_task_refs"]) != len(set(update["accepted_task_refs"])):
        raise ValueError("accepted_task_refs must be unique")
    if len(update["accepted_checkpoint_refs"]) != len(
        set(update["accepted_checkpoint_refs"])
    ):
        raise ValueError("accepted_checkpoint_refs must be unique")


def _resolve_task_ref(
    conn: Any,
    candidate_ref: str,
    initiative_card_id: int,
    initiative_id: str,
) -> tuple[str, int, str]:
    """Resolve a candidate ref to its lifecycle step.

    Returns (step, task_card_id, task_id).
    """
    row = conn.execute(
        """
        SELECT ch.task_card_id, ch.task_id, clc.step
        FROM task_candidate_handoffs ch
        JOIN task_reviewer_verdicts tvr ON ch.task_card_id = tvr.task_card_id
            AND ch.candidate_id = tvr.candidate_id
        JOIN task_lifecycle_contracts clc ON ch.task_card_id = clc.task_card_id
            AND ch.task_id = clc.task_id
        JOIN adrian_kanban_cards card
            ON card.id = ch.task_card_id
            AND card.task_id = ch.task_id
            AND card.card_type = 'task'
            AND card.initiative_id = ?
        WHERE ch.candidate_id = ?
          AND tvr.verdict = 'accepted'
          AND clc.initiative_card_id = ?
          AND clc.initiative_id = ?
        """,
        (initiative_id, candidate_ref, initiative_card_id, initiative_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "candidate ref does not resolve to an accepted handoff with valid "
            "lifecycle contract"
        )
    return row["step"], row["task_card_id"], row["task_id"]


def _validate_latest_transition(
    conn: Any,
    initiative_id: str,
    expected_phase: str,
) -> None:
    row = conn.execute(
        """
        SELECT to_phase, to_segment_id
        FROM initiative_transitions
        WHERE initiative_id = ?
        ORDER BY transition_id DESC
        LIMIT 1
        """,
        (initiative_id,),
    ).fetchone()
    if row is None:
        raise ValueError("no initiative transition found")
    if row["to_phase"] != expected_phase:
        raise ValueError("latest transition does not target the required phase")
    if row["to_segment_id"] is not None:
        raise ValueError("latest transition must have null segment")


def _validate_d4_3(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    resolved_steps: dict[str, str],
) -> None:
    result = update["result"]
    accepted_refs = update["accepted_task_refs"]

    if update["accepted_checkpoint_refs"]:
        raise ValueError("D4.3 requires no accepted checkpoint refs")

    if len(accepted_refs) != 2:
        raise ValueError("D4.3 requires exactly two accepted task refs")

    steps = [resolved_steps[ref] for ref in accepted_refs]
    if sorted(steps) != ["D4.1", "D4.2"]:
        raise ValueError("D4.3 requires accepted steps D4.1 and D4.2")

    if not _is_nonblank_str(result.get("write_gate_approval_ref")):
        raise ValueError("D4.3 result requires nonblank write_gate_approval_ref")
    digest = result.get("approved_change_set_digest")
    if not isinstance(digest, str) or not _HEX64_RE.fullmatch(digest):
        raise ValueError("D4.3 result requires valid 64-hex approved_change_set_digest")
    doc_refs = result.get("current_document_refs")
    if not isinstance(doc_refs, list) or not doc_refs:
        raise ValueError("D4.3 result requires nonempty current_document_refs")
    for ref in doc_refs:
        if not _is_nonblank_str(ref):
            raise ValueError("D4.3 current_document_refs must contain nonblank strings")

    from .phase_d4_evidence import validate_d4_source_pair

    author_ref = next(ref for ref in accepted_refs if resolved_steps[ref] == "D4.1")
    verifier_ref = next(ref for ref in accepted_refs if resolved_steps[ref] == "D4.2")
    author, verifier = validate_d4_source_pair(
        conn,
        initiative_card_id,
        initiative_id,
        author_ref,
        verifier_ref,
        digest,
    )
    if len(doc_refs) != len(set(doc_refs)):
        raise ValueError("D4.3 current_document_refs must be unique")
    expected_doc_refs = {
        edit["current_state_ref"] for edit in author.metadata["edit_set"]
    }
    if set(doc_refs) != expected_doc_refs:
        raise ValueError(
            "D4.3 current_document_refs must exactly match author edit_set current_state_refs"
        )


def _validate_d4_4(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
) -> None:
    result = update["result"]
    accepted_task_refs = update["accepted_task_refs"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]

    if accepted_task_refs:
        raise ValueError("D4.4 requires no accepted task refs")
    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("D4.4 requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    row = conn.execute(
        """
        SELECT canonical_payload, contract_id, contract_version, phase, segment_id, accepted
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'D4'
          AND segment_id IS NULL
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
        """,
        (checkpoint_ref, initiative_card_id, initiative_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "D4.4 checkpoint ref does not resolve to an accepted D4.3 checkpoint"
        )
    if (
        row["contract_id"] != _D4_CONTRACT_ID
        or row["contract_version"] != _CONTRACT_VERSION
    ):
        raise ValueError("D4.4 checkpoint ref contract mismatch")

    try:
        d4_3_payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("D4.3 checkpoint payload is invalid")
    if not isinstance(d4_3_payload, dict):
        raise ValueError("D4.3 checkpoint payload must be an object")
    if d4_3_payload.get("step") != "D4.3":
        raise ValueError("D4.4 checkpoint ref must reference a D4.3 step")

    if not _is_nonblank_str(d4_3_payload.get("write_gate_approval_ref")):
        raise ValueError(
            "D4.3 checkpoint payload requires valid write_gate_approval_ref"
        )
    d4_3_digest = d4_3_payload.get("approved_change_set_digest")
    if not isinstance(d4_3_digest, str) or not _HEX64_RE.fullmatch(d4_3_digest):
        raise ValueError(
            "D4.3 checkpoint payload requires valid approved_change_set_digest"
        )

    d4_3_approval_ref = d4_3_payload.get("write_gate_approval_ref")
    if result.get("write_gate_approval_ref") != d4_3_approval_ref:
        raise ValueError("D4.4 approval ref does not match D4.3")
    if result.get("approved_change_set_digest") != d4_3_digest:
        raise ValueError("D4.4 change set digest does not match D4.3")

    if not _is_nonblank_str(result.get("approval_lease_ref")):
        raise ValueError("D4.4 result requires nonblank approval_lease_ref")
    if not _is_nonblank_str(result.get("execution_result")):
        raise ValueError("D4.4 result requires nonblank execution_result")

    post_docs = result.get("post_write_documents")
    if not isinstance(post_docs, list) or not post_docs:
        raise ValueError("D4.4 result requires nonempty post_write_documents")
    for doc in post_docs:
        if not isinstance(doc, dict):
            raise ValueError("post_write_documents entries must be objects")
        if not _is_nonblank_str(doc.get("path")):
            raise ValueError("post_write_documents path must be nonblank")
        sha = doc.get("sha")
        if not isinstance(sha, str) or not _HEX40_RE.fullmatch(sha):
            raise ValueError("post_write_documents sha must be valid 40-hex")

    determinations = result.get("item_determinations")
    if not isinstance(determinations, list) or not determinations:
        raise ValueError("D4.4 result requires nonempty item_determinations")
    for det in determinations:
        if not isinstance(det, dict):
            raise ValueError("item_determinations entries must be objects")
        if not _is_nonblank_str(det.get("item_id")):
            raise ValueError("item_determinations item_id must be nonblank")
        if not _is_nonblank_str(det.get("determination")):
            raise ValueError("item_determinations determination must be nonblank")


def _validate_dev1_2(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    resolved_steps: dict[str, str],
) -> None:
    result = update["result"]
    accepted_refs = update["accepted_task_refs"]

    if update["accepted_checkpoint_refs"]:
        raise ValueError("DEV1.2 requires no accepted checkpoint refs")

    if len(accepted_refs) != 5:
        raise ValueError("DEV1.2 requires exactly five accepted task refs")

    expected_steps = [
        "DEV1.1a",
        "DEV1.1b",
        "DEV1.1c",
        "DEV1.1d",
        "DEV1.1e",
    ]
    steps = [resolved_steps[ref] for ref in accepted_refs]
    if sorted(steps) != sorted(expected_steps):
        raise ValueError("DEV1.2 requires accepted steps DEV1.1a through DEV1.1e")

    if not _is_nonblank_str(result.get("cumulative_record_ref")):
        raise ValueError("DEV1.2 result requires nonblank cumulative_record_ref")

    source_angles = result.get("source_angles")
    if not isinstance(source_angles, list) or len(source_angles) != 5:
        raise ValueError("DEV1.2 result requires exactly five source_angles")

    total_count = 0
    seen_candidate_refs: set[str] = set()
    for angle in source_angles:
        if not isinstance(angle, dict):
            raise ValueError("source_angles entries must be objects")
        step = angle.get("step")
        candidate_ref = angle.get("candidate_ref")
        item_count = angle.get("item_count")

        if not _is_nonblank_str(candidate_ref):
            raise ValueError("source_angles candidate_ref must be nonblank")
        if candidate_ref in seen_candidate_refs:
            raise ValueError("source_angles candidate_ref must be unique")
        seen_candidate_refs.add(candidate_ref)
        if candidate_ref not in accepted_refs:
            raise ValueError("source_angles candidate_ref not in accepted task refs")
        if resolved_steps.get(candidate_ref) != step:
            raise ValueError(
                "source_angles step does not match resolved lifecycle step"
            )
        if not _is_positive_int(item_count) and not (
            type(item_count) is int and item_count >= 0
        ):
            raise ValueError("source_angles item_count must be a nonnegative integer")
        total_count += item_count

    if seen_candidate_refs != set(accepted_refs):
        raise ValueError("source_angles candidate_refs must equal accepted_task_refs")

    total_item_count = result.get("total_item_count")
    if (
        type(total_item_count) is not int
        or isinstance(total_item_count, bool)
        or total_item_count < 0
    ):
        raise ValueError("DEV1.2 total_item_count must be a nonnegative integer")

    if result.get("total_item_count") != total_count:
        raise ValueError("DEV1.2 total_item_count does not match sum of source_angles")


def _validate_dev1_3(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    resolved_steps: dict[str, str],
) -> None:
    result = update["result"]
    accepted_task_refs = update["accepted_task_refs"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]

    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("DEV1.3 requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    row = conn.execute(
        """
        SELECT result_id, iteration, created_at, contract_id, contract_version,
               phase, segment_id, accepted, canonical_payload
        FROM initiative_phase_results
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV1'
          AND segment_id IS NULL
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = ?
          AND contract_version = ?
        ORDER BY iteration DESC, created_at DESC, result_id DESC
        LIMIT 1
        """,
        (initiative_card_id, initiative_id, _DEV1_CONTRACT_ID, _CONTRACT_VERSION),
    ).fetchone()
    if row is None:
        raise ValueError(
            "DEV1.3 checkpoint ref does not resolve to a valid DEV1.2 or DEV1.3 "
            "checkpoint"
        )
    if row["result_id"] != checkpoint_ref:
        raise ValueError("DEV1.3 checkpoint ref is not the latest accepted checkpoint")

    try:
        latest_payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("latest checkpoint payload is invalid")
    if not isinstance(latest_payload, dict):
        raise ValueError("latest checkpoint payload must be an object")
    if latest_payload.get("step") not in ("DEV1.2", "DEV1.3"):
        raise ValueError("latest checkpoint must be DEV1.2 or DEV1.3")

    if type(result.get("dry")) is not bool:
        raise ValueError("DEV1.3 result requires dry to be a boolean")

    predecessor_step = latest_payload.get("step")
    if predecessor_step == "DEV1.2":
        expected_cumulative_ref = latest_payload.get("cumulative_record_ref")
        if not _is_nonblank_str(expected_cumulative_ref):
            raise ValueError("DEV1.2 predecessor requires valid cumulative_record_ref")
        if result.get("pass_counter") != 1:
            raise ValueError("DEV1.3 pass_counter must be 1 when predecessor is DEV1.2")
    else:
        expected_cumulative_ref = latest_payload.get("updated_cumulative_record_ref")
        if not _is_nonblank_str(expected_cumulative_ref):
            raise ValueError(
                "DEV1.3 predecessor requires valid updated_cumulative_record_ref"
            )
        prior_pass_counter = latest_payload.get("pass_counter")
        if not _is_positive_int(prior_pass_counter):
            raise ValueError("DEV1.3 predecessor requires valid pass_counter")
        if result.get("pass_counter") != prior_pass_counter + 1:
            raise ValueError("DEV1.3 pass_counter must increment from predecessor")

    if result.get("current_cumulative_record_ref") != expected_cumulative_ref:
        raise ValueError(
            "DEV1.3 current_cumulative_record_ref does not match predecessor"
        )

    if not _is_positive_int(result.get("pass_counter")):
        raise ValueError("DEV1.3 result requires positive pass_counter")
    if not _is_nonblank_str(result.get("current_cumulative_record_ref")):
        raise ValueError(
            "DEV1.3 result requires nonblank current_cumulative_record_ref"
        )
    if not _is_nonblank_str(result.get("updated_cumulative_record_ref")):
        raise ValueError(
            "DEV1.3 result requires nonblank updated_cumulative_record_ref"
        )

    follow_up_refs = result.get("follow_up_task_refs")
    if not isinstance(follow_up_refs, list):
        raise ValueError("DEV1.3 result requires follow_up_task_refs list")
    for ref in follow_up_refs:
        if not _is_nonblank_str(ref):
            raise ValueError("follow_up_task_refs must contain nonblank strings")
    if len(follow_up_refs) != len(set(follow_up_refs)):
        raise ValueError("follow_up_task_refs must be unique")
    if set(follow_up_refs) != set(accepted_task_refs):
        raise ValueError("follow_up_task_refs must exactly equal accepted_task_refs")

    declarations = result.get("no_new_material_declarations")
    if not isinstance(declarations, list) or len(declarations) != 5:
        raise ValueError(
            "DEV1.3 result requires exactly five no_new_material_declarations"
        )

    expected_steps = [
        "DEV1.1a",
        "DEV1.1b",
        "DEV1.1c",
        "DEV1.1d",
        "DEV1.1e",
    ]
    seen_steps: set[str] = set()
    all_true = True
    for decl in declarations:
        if not isinstance(decl, dict):
            raise ValueError("no_new_material_declarations entries must be objects")
        step = decl.get("step")
        no_new_material = decl.get("no_new_material")
        if step not in expected_steps:
            raise ValueError("no_new_material_declarations step not recognized")
        if step in seen_steps:
            raise ValueError("duplicate no_new_material_declarations step")
        seen_steps.add(step)
        if not isinstance(no_new_material, bool):
            raise ValueError("no_new_material must be a boolean")
        if not no_new_material:
            all_true = False

    if seen_steps != set(expected_steps):
        raise ValueError(
            "no_new_material_declarations must cover DEV1.1a through DEV1.1e"
        )

    if result.get("dry") is True and not all_true:
        raise ValueError(
            "DEV1.3 dry run requires all no_new_material declarations to be true"
        )

    for ref in accepted_task_refs:
        step = resolved_steps.get(ref)
        if step not in expected_steps:
            raise ValueError(
                "DEV1.3 accepted task ref does not resolve to a DEV1.1a-e step"
            )


def admit_orchestration_checkpoint(
    conn: Any,
    *,
    initiative_card_id: int,
    initiative_id: str,
    actor_profile: str,
    update: dict[str, Any],
) -> CheckpointAdmission:
    if actor_profile != "default":
        raise ValueError("actor profile must be default")

    _validate_update_shape(update)

    result = update["result"]
    step = result.get("step")
    if not _is_nonblank_str(step) or step not in _RECOGNIZED_STEPS:
        raise ValueError("unrecognized checkpoint step")

    if step in _D4_STEPS:
        expected_phase = "D4"
        expected_contract = _D4_CONTRACT_ID
    else:
        expected_phase = "DEV1"
        expected_contract = _DEV1_CONTRACT_ID

    if update["phase"] != expected_phase:
        raise ValueError("phase does not match step")
    if update["contract_id"] != expected_contract:
        raise ValueError("contract_id does not match step")
    if update["contract_version"] != _CONTRACT_VERSION:
        raise ValueError("contract_version must be 1")

    _validate_latest_transition(conn, initiative_id, expected_phase)

    resolved_steps: dict[str, str] = {}
    for ref in update["accepted_task_refs"]:
        resolved_step, _, _ = _resolve_task_ref(
            conn, ref, initiative_card_id, initiative_id
        )
        resolved_steps[ref] = resolved_step

    if step == "D4.3":
        _validate_d4_3(conn, update, initiative_card_id, initiative_id, resolved_steps)
    elif step == "D4.4":
        _validate_d4_4(conn, update, initiative_card_id, initiative_id)
    elif step == "DEV1.2":
        _validate_dev1_2(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )
    elif step == "DEV1.3":
        _validate_dev1_3(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )

    return CheckpointAdmission(
        result_id=update["result_id"],
        step=step,
        phase=update["phase"],
        iteration=update["iteration"],
        contract_id=update["contract_id"],
        contract_version=update["contract_version"],
        result=result,
        accepted_task_refs=list(update["accepted_task_refs"]),
        accepted_checkpoint_refs=list(update["accepted_checkpoint_refs"]),
    )

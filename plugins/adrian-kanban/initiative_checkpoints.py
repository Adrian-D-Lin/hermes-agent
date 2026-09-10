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
_DEV2_STEPS = frozenset({"DEV2.3"})
_DEV3_STEPS = frozenset({"DEV3.9", "DEV3.12"})
_DEV4_STEPS = frozenset(
    {"DEV4.2b", "DEV4.2c", "DEV4.2d", "DEV4.2e", "DEV4.3", "DEV4.4", "DEV4.5"}
)
_RECOGNIZED_STEPS = (
    _D4_STEPS | _DEV1_STEPS | _DEV2_STEPS | _DEV3_STEPS | _DEV4_STEPS
)

_D4_CONTRACT_ID = "adrian-kanban.lifecycle.d4"
_DEV1_CONTRACT_ID = "adrian-kanban.lifecycle.dev1"
_DEV2_CONTRACT_ID = "adrian-kanban.lifecycle.dev2"
_DEV3_CONTRACT_ID = "adrian-kanban.lifecycle.dev3"
_DEV4_CONTRACT_ID = "adrian-kanban.lifecycle.dev4"
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
    segment_id = update.get("segment_id")
    if segment_id is not None and not _is_nonblank_str(segment_id):
        raise ValueError("segment_id must be null or a nonblank string")
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
    expected_segment_id: str | None = None,
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
    if row["to_segment_id"] != expected_segment_id:
        if expected_segment_id is None:
            raise ValueError("latest transition must have null segment")
        raise ValueError("latest transition does not target the required segment")


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


def _validate_dev2_3(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    resolved_steps: dict[str, str],
) -> None:
    result = update["result"]
    accepted_refs = update["accepted_task_refs"]
    segment_id = update["segment_id"]

    if update["accepted_checkpoint_refs"]:
        raise ValueError("DEV2.3 requires no accepted checkpoint refs")

    if len(accepted_refs) != 2:
        raise ValueError("DEV2.3 requires exactly two accepted task refs")

    steps = [resolved_steps[ref] for ref in accepted_refs]
    if sorted(steps) != ["DEV2.1", "DEV2.2"]:
        raise ValueError("DEV2.3 requires accepted steps DEV2.1 and DEV2.2")

    dev2_1_ref = next(ref for ref in accepted_refs if resolved_steps[ref] == "DEV2.1")
    dev2_2_ref = next(ref for ref in accepted_refs if resolved_steps[ref] == "DEV2.2")

    # Both lifecycle contract rows must name this initiative, segment, the same
    # nonblank workspace, the DEV2 contract/version, and the correct profiles.
    contract_rows = conn.execute(
        """
        SELECT clc.step, clc.segment_id, clc.workspace_id,
               clc.contract_id, clc.contract_version, clc.execution_profile,
               clc.canonical_contract_payload
        FROM task_lifecycle_contracts clc
        JOIN task_candidate_handoffs ch ON ch.task_card_id = clc.task_card_id
            AND ch.task_id = clc.task_id
        WHERE clc.initiative_card_id = ?
          AND clc.initiative_id = ?
          AND (ch.candidate_id = ? OR ch.candidate_id = ?)
        """,
        (initiative_card_id, initiative_id, dev2_1_ref, dev2_2_ref),
    ).fetchall()
    by_step = {row["step"]: row for row in contract_rows}
    if set(by_step) != {"DEV2.1", "DEV2.2"}:
        raise ValueError("DEV2.3 candidate contract rows must cover DEV2.1 and DEV2.2")
    dev2_1_row = by_step["DEV2.1"]
    dev2_2_row = by_step["DEV2.2"]
    for row in (dev2_1_row, dev2_2_row):
        if row["segment_id"] != segment_id:
            raise ValueError("DEV2.3 candidate contract segment mismatch")
        if not _is_nonblank_str(row["workspace_id"]):
            raise ValueError("DEV2.3 candidate contract workspace must be nonblank")
        if row["contract_id"] != _DEV2_CONTRACT_ID:
            raise ValueError("DEV2.3 candidate contract id mismatch")
        if row["contract_version"] != _CONTRACT_VERSION:
            raise ValueError("DEV2.3 candidate contract version mismatch")
    if dev2_1_row["workspace_id"] != dev2_2_row["workspace_id"]:
        raise ValueError("DEV2.3 candidates must share the same workspace")
    if dev2_1_row["execution_profile"] != "independent-reviewer":
        raise ValueError("DEV2.1 execution profile must be independent-reviewer")
    if dev2_2_row["execution_profile"] != "test-authority-reviewer":
        raise ValueError("DEV2.2 execution profile must be test-authority-reviewer")

    # DEV2.2's canonical contract payload must parse as an object and have
    # predecessor_ref exactly equal to the named DEV2.1 candidate.
    try:
        dev2_2_contract_payload = json.loads(dev2_2_row["canonical_contract_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("DEV2.2 contract payload is invalid")
    if not isinstance(dev2_2_contract_payload, dict):
        raise ValueError("DEV2.2 contract payload must be an object")
    if dev2_2_contract_payload.get("predecessor_ref") != dev2_1_ref:
        raise ValueError("DEV2.2 predecessor_ref must name the DEV2.1 candidate")

    # Parse both candidate metadata objects.
    def _candidate_metadata(candidate_ref: str) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT ch.metadata_json
            FROM task_candidate_handoffs ch
            WHERE ch.candidate_id = ?
            """,
            (candidate_ref,),
        ).fetchone()
        if row is None:
            raise ValueError("DEV2.3 candidate handoff not found")
        try:
            metadata = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            raise ValueError("DEV2.3 candidate metadata is invalid")
        if not isinstance(metadata, dict):
            raise ValueError("DEV2.3 candidate metadata must be an object")
        return metadata

    dev2_1_metadata = _candidate_metadata(dev2_1_ref)
    dev2_2_metadata = _candidate_metadata(dev2_2_ref)

    # Result must have exactly these fields.
    required_fields = {
        "step",
        "final_brief_candidate_ref",
        "final_check_candidate_ref",
        "final_brief_ref",
        "checked_brief_sha",
        "handoff_package_ref",
        "no_concealed_design_issue",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV2.3 result must contain exact required fields")

    if result.get("final_brief_candidate_ref") != dev2_1_ref:
        raise ValueError("DEV2.3 final_brief_candidate_ref must name the DEV2.1 candidate")
    if result.get("final_check_candidate_ref") != dev2_2_ref:
        raise ValueError("DEV2.3 final_check_candidate_ref must name the DEV2.2 candidate")

    final_brief_ref = result.get("final_brief_ref")
    if not _is_nonblank_str(final_brief_ref):
        raise ValueError("DEV2.3 final_brief_ref must be a nonblank string")
    if final_brief_ref != dev2_1_metadata.get("brief_ref"):
        raise ValueError("DEV2.3 final_brief_ref must equal DEV2.1 brief_ref")

    checked_sha = result.get("checked_brief_sha")
    if not isinstance(checked_sha, str) or not _HEX40_RE.fullmatch(checked_sha):
        raise ValueError("DEV2.3 checked_brief_sha must be 40 lowercase hex")
    if "@" not in final_brief_ref:
        raise ValueError("DEV2.3 final_brief_ref must include a @ suffix")
    sha_suffix = final_brief_ref.rsplit("@", 1)[-1]
    if checked_sha != sha_suffix:
        raise ValueError("DEV2.3 checked_brief_sha must be the SHA suffix of final_brief_ref")

    if not _is_nonblank_str(result.get("handoff_package_ref")):
        raise ValueError("DEV2.3 handoff_package_ref must be a nonblank string")

    if result.get("no_concealed_design_issue") is not True:
        raise ValueError("DEV2.3 no_concealed_design_issue must be true")

    if result.get("next_route") != "DEV3":
        raise ValueError("DEV2.3 next_route must be DEV3")

    # DEV2.2 metadata must have conclusion == 'ACCEPTED' and findings == [].
    if dev2_2_metadata.get("conclusion") != "ACCEPTED":
        raise ValueError("DEV2.2 final check conclusion must be ACCEPTED")
    if dev2_2_metadata.get("findings") != []:
        raise ValueError("DEV2.2 final check findings must be empty")


def _validate_dev3_9(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    resolved_steps: dict[str, str],
) -> None:
    result = update["result"]
    accepted_refs = update["accepted_task_refs"]
    segment_id = update["segment_id"]

    if update["accepted_checkpoint_refs"]:
        raise ValueError("DEV3.9 requires no accepted checkpoint refs")

    if len(accepted_refs) != 8:
        raise ValueError("DEV3.9 requires exactly eight accepted task refs")

    expected_steps = [f"DEV3.{index}" for index in range(1, 9)]
    steps = [resolved_steps[ref] for ref in accepted_refs]
    if sorted(steps) != expected_steps:
        raise ValueError("DEV3.9 requires accepted steps DEV3.1 through DEV3.8")

    by_step = {resolved_steps[ref]: ref for ref in accepted_refs}

    profiles = {
        "DEV3.1": "builder-tester",
        "DEV3.2": "builder-tester",
        "DEV3.3": "test-authority-reviewer",
        "DEV3.4": "test-authority-reviewer",
        "DEV3.5": "test-authority-reviewer",
        "DEV3.6": "independent-reviewer",
        "DEV3.7": "independent-reviewer",
        "DEV3.8": "independent-reviewer",
    }
    contract_rows = conn.execute(
        """
        SELECT clc.step, clc.segment_id, clc.workspace_id,
               clc.contract_id, clc.contract_version, clc.execution_profile,
               clc.canonical_contract_payload
        FROM task_lifecycle_contracts clc
        JOIN task_candidate_handoffs ch ON ch.task_card_id = clc.task_card_id
            AND ch.task_id = clc.task_id
        WHERE clc.initiative_card_id = ?
          AND clc.initiative_id = ?
          AND (ch.candidate_id = ? OR ch.candidate_id = ?
               OR ch.candidate_id = ? OR ch.candidate_id = ?
               OR ch.candidate_id = ? OR ch.candidate_id = ?
               OR ch.candidate_id = ? OR ch.candidate_id = ?)
        """,
        (
            initiative_card_id,
            initiative_id,
            by_step["DEV3.1"],
            by_step["DEV3.2"],
            by_step["DEV3.3"],
            by_step["DEV3.4"],
            by_step["DEV3.5"],
            by_step["DEV3.6"],
            by_step["DEV3.7"],
            by_step["DEV3.8"],
        ),
    ).fetchall()
    rows_by_step = {row["step"]: row for row in contract_rows}
    if set(rows_by_step) != set(expected_steps):
        raise ValueError("DEV3.9 candidate contract rows must cover DEV3.1 through DEV3.8")
    workspaces = set()
    for step in expected_steps:
        row = rows_by_step[step]
        if row["segment_id"] != segment_id:
            raise ValueError("DEV3.9 candidate contract segment mismatch")
        if not _is_nonblank_str(row["workspace_id"]):
            raise ValueError("DEV3.9 candidate contract workspace must be nonblank")
        workspaces.add(row["workspace_id"])
        if row["contract_id"] != _DEV3_CONTRACT_ID:
            raise ValueError("DEV3.9 candidate contract id mismatch")
        if row["contract_version"] != _CONTRACT_VERSION:
            raise ValueError("DEV3.9 candidate contract version mismatch")
        if row["execution_profile"] != profiles[step]:
            raise ValueError(f"DEV3.9 candidate {step} execution profile mismatch")
    if len(workspaces) != 1:
        raise ValueError("DEV3.9 candidates must share the same workspace")

    # The canonical contract predecessor chain is exact: DEV3.2 names the
    # DEV3.1 candidate, DEV3.3 names DEV3.2, through DEV3.8 naming DEV3.7.
    # DEV3.1 may name its prior DEV2 checkpoint and only needs a nonblank
    # predecessor.
    def _contract_predecessor(step: str) -> Any:
        try:
            payload = json.loads(rows_by_step[step]["canonical_contract_payload"])
        except (json.JSONDecodeError, TypeError):
            raise ValueError(f"DEV3.9 candidate {step} contract payload is invalid")
        if not isinstance(payload, dict):
            raise ValueError(f"DEV3.9 candidate {step} contract payload must be an object")
        return payload.get("predecessor_ref")

    for step in expected_steps[1:]:
        if _contract_predecessor(step) != by_step[f"DEV3.{int(step.split('.')[1]) - 1}"]:
            raise ValueError(f"DEV3.9 candidate {step} predecessor_ref does not name the prior step")
    if not _is_nonblank_str(_contract_predecessor("DEV3.1")):
        raise ValueError("DEV3.9 candidate DEV3.1 predecessor_ref must be nonblank")

    def _candidate_metadata(candidate_ref: str) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT ch.metadata_json
            FROM task_candidate_handoffs ch
            WHERE ch.candidate_id = ?
            """,
            (candidate_ref,),
        ).fetchone()
        if row is None:
            raise ValueError("DEV3.9 candidate handoff not found")
        try:
            metadata = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            raise ValueError("DEV3.9 candidate metadata is invalid")
        if not isinstance(metadata, dict):
            raise ValueError("DEV3.9 candidate metadata must be an object")
        return metadata

    metadata = {step: _candidate_metadata(by_step[step]) for step in expected_steps}

    required_fields = {
        "step",
        "cycle",
        "accepted_build_ref",
        "integrated_review_candidate_ref",
        "classification_record_ref",
        "classifications",
        "unresolved_remediable_count",
        "temporary_material_refs",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV3.9 result must contain exact required fields")

    if not _is_positive_int(result.get("cycle")) or result.get("cycle") != update["iteration"]:
        raise ValueError("DEV3.9 cycle must equal the update iteration")

    if not _is_nonblank_str(result.get("accepted_build_ref")):
        raise ValueError("DEV3.9 accepted_build_ref must be a nonblank string")
    if result.get("accepted_build_ref") != metadata["DEV3.1"].get("build_ref"):
        raise ValueError("DEV3.9 accepted_build_ref must equal DEV3.1 build_ref")

    if result.get("integrated_review_candidate_ref") != by_step["DEV3.8"]:
        raise ValueError("DEV3.9 integrated_review_candidate_ref must name the DEV3.8 candidate")

    if not _is_nonblank_str(result.get("classification_record_ref")):
        raise ValueError("DEV3.9 classification_record_ref must be a nonblank string")

    classifications = result.get("classifications")
    if not isinstance(classifications, list):
        raise ValueError("DEV3.9 classifications must be a list")

    unresolved_remediable_count = result.get("unresolved_remediable_count")
    if (
        type(unresolved_remediable_count) is not int
        or isinstance(unresolved_remediable_count, bool)
        or unresolved_remediable_count < 0
    ):
        raise ValueError("DEV3.9 unresolved_remediable_count must be a nonnegative integer")

    temporary_material_refs = result.get("temporary_material_refs")
    if not isinstance(temporary_material_refs, list):
        raise ValueError("DEV3.9 temporary_material_refs must be a list")
    for ref in temporary_material_refs:
        if not _is_nonblank_str(ref):
            raise ValueError("DEV3.9 temporary_material_refs must contain nonblank strings")
    if len(temporary_material_refs) != len(set(temporary_material_refs)):
        raise ValueError("DEV3.9 temporary_material_refs must be unique")

    # Derive the exact set of finding references that require classification:
    # each item in DEV3.3/DEV3.5/DEV3.6/DEV3.7 findings and each item in
    # DEV3.4 failures, referenced by their stable zero-based index.
    derived_refs: list[str] = []
    for step in ("DEV3.3", "DEV3.5", "DEV3.6", "DEV3.7"):
        findings = metadata[step].get("findings")
        if not isinstance(findings, list):
            raise ValueError(f"DEV3.9 requires {step} findings to be a list")
        for index in range(len(findings)):
            derived_refs.append(f"{by_step[step]}#findings[{index}]")
    dev3_4_failures = metadata["DEV3.4"].get("failures")
    if not isinstance(dev3_4_failures, list):
        raise ValueError("DEV3.9 requires DEV3.4 failures to be a list")
    for index in range(len(dev3_4_failures)):
        derived_refs.append(f"{by_step['DEV3.4']}#failures[{index}]")
    derived_ref_set = set(derived_refs)

    # Review-step conclusions must stay consistent with whether they contain
    # findings: empty means PASSED, nonempty means not PASSED.
    for step in ("DEV3.3", "DEV3.5", "DEV3.6", "DEV3.7"):
        findings = metadata[step].get("findings")
        conclusion = metadata[step].get("conclusion")
        if findings == []:
            if conclusion != "PASSED":
                raise ValueError(f"DEV3.9 requires {step} conclusion PASSED when findings are empty")
        elif conclusion == "PASSED":
            raise ValueError(f"DEV3.9 requires {step} conclusion not PASSED when findings exist")

    dev3_4 = metadata["DEV3.4"]
    suite_results = dev3_4.get("suite_results")
    if not isinstance(suite_results, list) or len(suite_results) != 3:
        raise ValueError("DEV3.9 requires DEV3.4 to report exactly three suites")
    seen_suites = set()
    total_failed = 0
    for entry in suite_results:
        if not isinstance(entry, dict):
            raise ValueError("DEV3.4 suite_results entries must be objects")
        suite = entry.get("suite")
        if suite not in {"happy", "unhappy", "fringe"}:
            raise ValueError("DEV3.9 requires happy/unhappy/fringe suite names")
        if suite in seen_suites:
            raise ValueError("DEV3.4 suite names must be unique")
        seen_suites.add(suite)
        if not _is_positive_int(entry.get("passed")):
            raise ValueError("DEV3.4 suite passed count must be a positive integer")
        failed = entry.get("failed")
        if type(failed) is not int or isinstance(failed, bool) or failed < 0:
            raise ValueError("DEV3.4 suite failed count must be a nonnegative integer")
        total_failed += failed
    if seen_suites != {"happy", "unhappy", "fringe"}:
        raise ValueError("DEV3.9 requires happy/unhappy/fringe suite names")
    if total_failed != len(dev3_4_failures):
        raise ValueError("DEV3.9 requires total DEV3.4 failed count to equal the number of failure items")

    dev3_8 = metadata["DEV3.8"]
    dispositions = dev3_8.get("dispositions")
    if not isinstance(dispositions, list):
        raise ValueError("DEV3.9 requires DEV3.8 dispositions to be a list")
    disposition_refs = [item.get("finding_ref") if isinstance(item, dict) else None for item in dispositions]
    if len(disposition_refs) != len(set(disposition_refs)) or any(ref is None for ref in disposition_refs):
        raise ValueError("DEV3.9 requires DEV3.8 dispositions to name each finding reference once")
    if set(disposition_refs) != derived_ref_set:
        raise ValueError("DEV3.9 requires DEV3.8 dispositions to name exactly the derived finding references")
    if dev3_8.get("unresolved_count") != len(derived_refs):
        raise ValueError("DEV3.9 requires DEV3.8 unresolved_count to equal the number of derived findings")

    # Classifications must account for exactly the same finding refs once each.
    classification_refs = []
    remediable_count = 0
    has_ambiguity = False
    has_design_gap = False
    has_build_bug = False
    for entry in classifications:
        if not isinstance(entry, dict):
            raise ValueError("DEV3.9 classifications entries must be objects")
        if set(entry.keys()) != {"finding_ref", "classification", "evidence_refs", "route"}:
            raise ValueError("DEV3.9 classification entries must have exactly finding_ref, classification, evidence_refs, and route")
        if not _is_nonblank_str(entry.get("finding_ref")):
            raise ValueError("DEV3.9 classification finding_ref must be a nonblank string")
        classification_refs.append(entry["finding_ref"])
        classification = entry.get("classification")
        route = entry.get("route")
        if classification in ("build_bug", "test_defect"):
            expected_route = "DEV3.10"
            remediable_count += 1
            has_build_bug = True
        elif classification in ("design_gap", "design_relevant_decision"):
            expected_route = "D1"
            has_design_gap = True
        elif classification == "design_ambiguity":
            expected_route = "HUMAN_DECISION"
            has_ambiguity = True
        elif classification == "implementation_detail":
            expected_route = "RECORD_ONLY"
        else:
            raise ValueError("DEV3.9 classification value not recognized")
        if entry.get("route") != expected_route:
            raise ValueError("DEV3.9 classification route does not match its classification")
        evidence_refs = entry.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise ValueError("DEV3.9 classification evidence_refs must be a nonempty list")
        for ref in evidence_refs:
            if not _is_nonblank_str(ref):
                raise ValueError("DEV3.9 classification evidence_refs must contain nonblank strings")
        if len(evidence_refs) != len(set(evidence_refs)):
            raise ValueError("DEV3.9 classification evidence_refs must be unique")
    if len(classification_refs) != len(set(classification_refs)):
        raise ValueError("DEV3.9 classifications must account for each finding reference once")
    if set(classification_refs) != derived_ref_set:
        raise ValueError("DEV3.9 classifications must account for exactly the derived finding references")
    if unresolved_remediable_count != remediable_count:
        raise ValueError("DEV3.9 unresolved_remediable_count must equal the build bug/test defect count")

    if has_ambiguity:
        expected_next_route = "HUMAN_DECISION"
    elif has_design_gap:
        expected_next_route = "D1"
    elif has_build_bug:
        expected_next_route = "DEV3.10"
    else:
        expected_next_route = "DEV4"
    if result.get("next_route") != expected_next_route:
        raise ValueError("DEV3.9 next_route does not match the derived priority route")


def _validate_dev3_12(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    resolved_steps: dict[str, str],
) -> None:
    result = update["result"]
    accepted_refs = update["accepted_task_refs"]
    segment_id = update["segment_id"]

    checkpoint_refs = update["accepted_checkpoint_refs"]
    if len(checkpoint_refs) != 1:
        raise ValueError("DEV3.12 requires exactly one accepted checkpoint ref")
    prior_ref = checkpoint_refs[0]

    if len(accepted_refs) != 2:
        raise ValueError("DEV3.12 requires exactly two accepted task refs")

    steps = [resolved_steps[ref] for ref in accepted_refs]
    if sorted(steps) != ["DEV3.10", "DEV3.11"]:
        raise ValueError("DEV3.12 requires accepted steps DEV3.10 and DEV3.11")

    by_step = {resolved_steps[ref]: ref for ref in accepted_refs}

    # The cited checkpoint must be an accepted DEV3.9 orchestration checkpoint
    # for this exact card, initiative, and segment.
    checkpoint_row = conn.execute(
        """
        SELECT ipr.phase, ipr.segment_id, ipr.initiative_card_id, ipr.iteration,
               ipr.accepted, ipr.canonical_payload
        FROM initiative_phase_results ipr
        WHERE ipr.result_id = ?
          AND ipr.initiative_id = ?
          AND ipr.result_kind = 'orchestration_checkpoint'
        """,
        (prior_ref, initiative_id),
    ).fetchone()
    if checkpoint_row is None:
        raise ValueError("DEV3.12 cited checkpoint not found")
    if checkpoint_row["phase"] != "DEV3":
        raise ValueError("DEV3.12 cited checkpoint must be a DEV3 checkpoint")
    if checkpoint_row["initiative_card_id"] != initiative_card_id:
        raise ValueError("DEV3.12 cited checkpoint must belong to this initiative card")
    if checkpoint_row["segment_id"] != segment_id:
        raise ValueError("DEV3.12 cited checkpoint must belong to this segment")
    if checkpoint_row["accepted"] != 1:
        raise ValueError("DEV3.12 cited checkpoint must be accepted")
    if checkpoint_row["iteration"] != update["iteration"] - 1:
        raise ValueError("DEV3.12 cited checkpoint iteration must be one less than this update")

    try:
        prior_result = json.loads(checkpoint_row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("DEV3.12 cited checkpoint payload is invalid")
    if not isinstance(prior_result, dict):
        raise ValueError("DEV3.12 cited checkpoint payload must be an object")
    if prior_result.get("step") != "DEV3.9":
        raise ValueError("DEV3.12 cited checkpoint payload step must be DEV3.9")
    if prior_result.get("next_route") != "DEV3.10":
        raise ValueError("DEV3.12 cited checkpoint payload next_route must be DEV3.10")
    classifications = prior_result.get("classifications")
    if not isinstance(classifications, list):
        raise ValueError("DEV3.12 cited checkpoint classifications must be a list")
    remediable_count = sum(
        1
        for entry in classifications
        if isinstance(entry, dict)
        and entry.get("classification") in ("build_bug", "test_defect")
    )
    if remediable_count < 1:
        raise ValueError("DEV3.12 cited checkpoint must have at least one remediable classification")

    # Both revision task contract rows must name the exact initiative/segment,
    # the same nonblank workspace, the DEV3 contract/version, and the
    # builder-tester profile.
    dev3_10_ref = by_step["DEV3.10"]
    dev3_11_ref = by_step["DEV3.11"]
    contract_rows = conn.execute(
        """
        SELECT clc.step, clc.segment_id, clc.workspace_id,
               clc.contract_id, clc.contract_version, clc.execution_profile,
               clc.canonical_contract_payload
        FROM task_lifecycle_contracts clc
        JOIN task_candidate_handoffs ch ON ch.task_card_id = clc.task_card_id
            AND ch.task_id = clc.task_id
        WHERE clc.initiative_card_id = ?
          AND clc.initiative_id = ?
          AND (ch.candidate_id = ? OR ch.candidate_id = ?)
        """,
        (initiative_card_id, initiative_id, dev3_10_ref, dev3_11_ref),
    ).fetchall()
    rows_by_step = {row["step"]: row for row in contract_rows}
    if set(rows_by_step) != {"DEV3.10", "DEV3.11"}:
        raise ValueError("DEV3.12 candidate contract rows must cover DEV3.10 and DEV3.11")
    dev3_10_row = rows_by_step["DEV3.10"]
    dev3_11_row = rows_by_step["DEV3.11"]
    for row in (dev3_10_row, dev3_11_row):
        if row["segment_id"] != segment_id:
            raise ValueError("DEV3.12 candidate contract segment mismatch")
        if not _is_nonblank_str(row["workspace_id"]):
            raise ValueError("DEV3.12 candidate contract workspace must be nonblank")
        if row["contract_id"] != _DEV3_CONTRACT_ID:
            raise ValueError("DEV3.12 candidate contract id mismatch")
        if row["contract_version"] != _CONTRACT_VERSION:
            raise ValueError("DEV3.12 candidate contract version mismatch")
        if row["execution_profile"] != "builder-tester":
            raise ValueError("DEV3.12 candidate execution profile must be builder-tester")
    if dev3_10_row["workspace_id"] != dev3_11_row["workspace_id"]:
        raise ValueError("DEV3.12 candidates must share the same workspace")

    def _contract_predecessor(step: str) -> Any:
        try:
            payload = json.loads(rows_by_step[step]["canonical_contract_payload"])
        except (json.JSONDecodeError, TypeError):
            raise ValueError(f"DEV3.12 candidate {step} contract payload is invalid")
        if not isinstance(payload, dict):
            raise ValueError(f"DEV3.12 candidate {step} contract payload must be an object")
        return payload.get("predecessor_ref")

    if _contract_predecessor("DEV3.10") != prior_ref:
        raise ValueError("DEV3.10 predecessor_ref must name the cited DEV3.9 checkpoint")
    if _contract_predecessor("DEV3.11") != dev3_10_ref:
        raise ValueError("DEV3.11 predecessor_ref must name the DEV3.10 candidate")

    def _candidate_metadata(candidate_ref: str) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT ch.metadata_json
            FROM task_candidate_handoffs ch
            WHERE ch.candidate_id = ?
            """,
            (candidate_ref,),
        ).fetchone()
        if row is None:
            raise ValueError("DEV3.12 candidate handoff not found")
        try:
            metadata = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            raise ValueError("DEV3.12 candidate metadata is invalid")
        if not isinstance(metadata, dict):
            raise ValueError("DEV3.12 candidate metadata must be an object")
        return metadata

    dev3_10_metadata = _candidate_metadata(dev3_10_ref)
    dev3_11_metadata = _candidate_metadata(dev3_11_ref)

    required_fields = {
        "step",
        "cycle",
        "prior_classification_checkpoint_ref",
        "revision_brief_candidate_ref",
        "revision_build_candidate_ref",
        "revised_build_ref",
        "full_cycle_steps",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV3.12 result must contain exact required fields")

    if not _is_positive_int(result.get("cycle")) or result.get("cycle") != update["iteration"]:
        raise ValueError("DEV3.12 cycle must equal the update iteration")

    if result.get("prior_classification_checkpoint_ref") != prior_ref:
        raise ValueError("DEV3.12 prior_classification_checkpoint_ref must name the cited checkpoint")
    if result.get("revision_brief_candidate_ref") != dev3_10_ref:
        raise ValueError("DEV3.12 revision_brief_candidate_ref must name the DEV3.10 candidate")
    if result.get("revision_build_candidate_ref") != dev3_11_ref:
        raise ValueError("DEV3.12 revision_build_candidate_ref must name the DEV3.11 candidate")

    revised_build_ref = result.get("revised_build_ref")
    if not _is_nonblank_str(revised_build_ref):
        raise ValueError("DEV3.12 revised_build_ref must be a nonblank string")
    if revised_build_ref != dev3_11_metadata.get("build_ref"):
        raise ValueError("DEV3.12 revised_build_ref must equal DEV3.11 build_ref")

    if not _is_nonblank_str(dev3_10_metadata.get("revision_brief_ref")):
        raise ValueError("DEV3.10 metadata revision_brief_ref must be nonblank")
    finding_changes = dev3_10_metadata.get("finding_changes")
    if not isinstance(finding_changes, list) or not finding_changes:
        raise ValueError("DEV3.10 metadata finding_changes must be a nonempty list")

    revision_evidence = dev3_11_metadata.get("revision_evidence")
    if not isinstance(revision_evidence, list) or not revision_evidence:
        raise ValueError("DEV3.11 metadata revision_evidence must be a nonempty list")

    full_cycle_steps = result.get("full_cycle_steps")
    if full_cycle_steps != [f"DEV3.{index}" for index in range(1, 10)]:
        raise ValueError("DEV3.12 full_cycle_steps must be exactly DEV3.1 through DEV3.9")

    if result.get("next_route") != "DEV3.1":
        raise ValueError("DEV3.12 next_route must be DEV3.1")


def _validate_dev4_candidate_contract_rows(
    conn: Any,
    initiative_card_id: int,
    initiative_id: str,
    segment_id: str,
    refs: list[str],
    expected_steps: set[str],
    profiles: dict[str, str],
) -> dict[str, Any]:
    placeholders = " OR ".join(["ch.candidate_id = ?"] * len(refs))
    rows = conn.execute(
        f"""
        SELECT clc.step, clc.segment_id, clc.workspace_id,
               clc.contract_id, clc.contract_version, clc.execution_profile,
               clc.canonical_contract_payload
        FROM task_lifecycle_contracts clc
        JOIN task_candidate_handoffs ch ON ch.task_card_id = clc.task_card_id
            AND ch.task_id = clc.task_id
        WHERE clc.initiative_card_id = ?
          AND clc.initiative_id = ?
          AND ({placeholders})
        """,
        (initiative_card_id, initiative_id, *refs),
    ).fetchall()
    by_step = {row["step"]: row for row in rows}
    if set(by_step) != expected_steps:
        raise ValueError("DEV4.2b candidate contract rows must cover the cited steps")
    workspaces: set[str] = set()
    for step in sorted(expected_steps):
        row = by_step[step]
        if row["segment_id"] != segment_id:
            raise ValueError("DEV4.2b candidate contract segment mismatch")
        if not _is_nonblank_str(row["workspace_id"]):
            raise ValueError("DEV4.2b candidate contract workspace must be nonblank")
        workspaces.add(row["workspace_id"])
        if row["contract_id"] != _DEV4_CONTRACT_ID:
            raise ValueError("DEV4.2b candidate contract id mismatch")
        if row["contract_version"] != _CONTRACT_VERSION:
            raise ValueError("DEV4.2b candidate contract version mismatch")
        if row["execution_profile"] != profiles[step]:
            raise ValueError(f"DEV4.2b candidate {step} execution profile mismatch")
    if len(workspaces) != 1:
        raise ValueError("DEV4.2b candidates must share the same workspace")
    return by_step


def _dev4_contract_predecessor(row: Any, step: str) -> Any:
    try:
        payload = json.loads(row["canonical_contract_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError(f"DEV4.2b candidate {step} contract payload is invalid")
    if not isinstance(payload, dict):
        raise ValueError(f"DEV4.2b candidate {step} contract payload must be an object")
    return payload.get("predecessor_ref")


def _dev4_candidate_metadata(conn: Any, candidate_ref: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT ch.metadata_json
        FROM task_candidate_handoffs ch
        WHERE ch.candidate_id = ?
        """,
        (candidate_ref,),
    ).fetchone()
    if row is None:
        raise ValueError("DEV4.2b candidate handoff not found")
    try:
        metadata = json.loads(row["metadata_json"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("DEV4.2b candidate metadata is invalid")
    if not isinstance(metadata, dict):
        raise ValueError("DEV4.2b candidate metadata must be an object")
    return metadata


def _resolve_dev4_prior_checkpoint(
    conn: Any,
    checkpoint_ref: str,
    initiative_card_id: int,
    initiative_id: str,
    segment_id: str,
    expected_step: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT result_id, iteration, created_at, contract_id, contract_version,
               phase, segment_id, accepted, canonical_payload
        FROM initiative_phase_results
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV4'
          AND segment_id = ?
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = ?
          AND contract_version = ?
        ORDER BY iteration DESC, created_at DESC, result_id DESC
        LIMIT 1
        """,
        (
            initiative_card_id,
            initiative_id,
            segment_id,
            _DEV4_CONTRACT_ID,
            _CONTRACT_VERSION,
        ),
    ).fetchone()
    if row is None:
        raise ValueError(f"{expected_step} prior checkpoint does not resolve to a valid DEV4 checkpoint")
    if row["result_id"] != checkpoint_ref:
        raise ValueError(f"{expected_step} checkpoint ref is not the latest accepted DEV4 checkpoint")
    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError(f"{expected_step} prior checkpoint payload is invalid")
    if not isinstance(payload, dict):
        raise ValueError(f"{expected_step} prior checkpoint payload must be an object")
    if payload.get("step") != expected_step:
        raise ValueError(f"{expected_step} prior checkpoint payload step must be {expected_step}")
    return payload


def _validate_dev4_2b(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    resolved_steps: dict[str, str],
) -> None:
    result = update["result"]
    accepted_refs = update["accepted_task_refs"]
    segment_id = update["segment_id"]

    if update["accepted_checkpoint_refs"]:
        raise ValueError("DEV4.2b requires no accepted checkpoint refs")
    if len(accepted_refs) != 1:
        raise ValueError("DEV4.2b requires exactly one accepted task ref")

    dev4_2a_ref = accepted_refs[0]
    if resolved_steps[dev4_2a_ref] != "DEV4.2a":
        raise ValueError("DEV4.2b accepted task ref must resolve to DEV4.2a")

    expected_chain_steps = {
        "DEV4.1a": "candidate:DEV4.1a",
        "DEV4.1b": "candidate:DEV4.1b",
        "DEV4.1c": "candidate:DEV4.1c",
        "DEV4.2a": dev4_2a_ref,
    }
    profiles = {
        "DEV4.1a": "independent-reviewer",
        "DEV4.1b": "test-authority-reviewer",
        "DEV4.1c": "builder-tester",
        "DEV4.2a": "independent-reviewer",
    }
    contract_rows = _validate_dev4_candidate_contract_rows(
        conn,
        initiative_card_id,
        initiative_id,
        segment_id,
        list(expected_chain_steps.values()),
        set(expected_chain_steps),
        profiles,
    )
    if not _is_nonblank_str(_dev4_contract_predecessor(contract_rows["DEV4.1a"], "DEV4.1a")):
        raise ValueError("DEV4.1a predecessor_ref must be nonblank")
    for step, expected_ref in (
        ("DEV4.1b", "candidate:DEV4.1a"),
        ("DEV4.1c", "candidate:DEV4.1b"),
        ("DEV4.2a", "candidate:DEV4.1c"),
    ):
        if _dev4_contract_predecessor(contract_rows[step], step) != expected_ref:
            raise ValueError(f"DEV4.2b candidate {step} predecessor_ref does not name the prior step")

    metadata_by_step = {
        step: _dev4_candidate_metadata(conn, ref)
        for step, ref in expected_chain_steps.items()
    }
    dev4_1b = metadata_by_step["DEV4.1b"]
    if dev4_1b.get("overreach_findings") != [] or dev4_1b.get("underreach_findings") != []:
        raise ValueError("DEV4.1b archive review findings must be empty")
    if dev4_1b.get("conclusion") != "VERIFIED":
        raise ValueError("DEV4.1b archive review conclusion must be VERIFIED")
    if not isinstance(metadata_by_step["DEV4.1c"].get("executed_dispositions"), list):
        raise ValueError("DEV4.1c executed_dispositions must be a list")
    observations = metadata_by_step["DEV4.2a"].get("observations")
    if not isinstance(observations, list):
        raise ValueError("DEV4.2a observations must be a list")

    required_fields = {
        "step",
        "ratification_package_candidate_ref",
        "observation_refs",
        "integration_review_ref",
        "ratification_presentation_ref",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV4.2b result must contain exact required fields")

    if result.get("ratification_package_candidate_ref") != dev4_2a_ref:
        raise ValueError("DEV4.2b ratification_package_candidate_ref must name the DEV4.2a candidate")
    expected_observation_refs = [f"{dev4_2a_ref}#observations[{index}]" for index in range(len(observations))]
    if result.get("observation_refs") != expected_observation_refs:
        raise ValueError("DEV4.2b observation_refs must exactly match DEV4.2a observations in order")
    if not _is_nonblank_str(result.get("integration_review_ref")):
        raise ValueError("DEV4.2b integration_review_ref must be a nonblank string")
    if not _is_nonblank_str(result.get("ratification_presentation_ref")):
        raise ValueError("DEV4.2b ratification_presentation_ref must be a nonblank string")
    if result.get("next_route") != "DEV4.2c":
        raise ValueError("DEV4.2b next_route must be DEV4.2c")


def _validate_dev4_2c(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    approval_id: str | None,
) -> None:
    result = update["result"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]
    segment_id = update["segment_id"]

    if update["accepted_task_refs"]:
        raise ValueError("DEV4.2c requires no accepted task refs")
    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("DEV4.2c requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    prior_payload = _resolve_dev4_prior_checkpoint(
        conn,
        checkpoint_ref,
        initiative_card_id,
        initiative_id,
        segment_id,
        "DEV4.2b",
    )

    if not _is_nonblank_str(approval_id):
        raise ValueError("DEV4.2c requires the outer initiative mutation approval ID")
    if result.get("human_approval_ref") != approval_id:
        raise ValueError("DEV4.2c human_approval_ref must equal the outer approval ID")

    required_fields = {
        "step",
        "integration_review_checkpoint_ref",
        "human_approval_ref",
        "decisions",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV4.2c result must contain exact required fields")
    if result.get("integration_review_checkpoint_ref") != checkpoint_ref:
        raise ValueError("DEV4.2c integration_review_checkpoint_ref must name the cited checkpoint")

    observation_refs = prior_payload.get("observation_refs")
    if not isinstance(observation_refs, list):
        raise ValueError("DEV4.2b predecessor observation_refs must be a list")

    decisions = result.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("DEV4.2c result requires decisions list")
    seen_refs: list[str] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("DEV4.2c decisions entries must be objects")
        if set(decision.keys()) != {"observation_ref", "outcome", "rationale", "target_ref"}:
            raise ValueError("DEV4.2c decision entries must have exactly observation_ref, outcome, rationale, and target_ref")
        if not _is_nonblank_str(decision.get("observation_ref")):
            raise ValueError("DEV4.2c decision observation_ref must be a nonblank string")
        seen_refs.append(decision["observation_ref"])
        if decision.get("outcome") not in ("Reflect", "Drop", "Defer", "Reject"):
            raise ValueError("DEV4.2c decision outcome must be Reflect, Drop, Defer, or Reject")
        if not _is_nonblank_str(decision.get("rationale")):
            raise ValueError("DEV4.2c decision rationale must be a nonblank string")
        if not _is_nonblank_str(decision.get("target_ref")):
            raise ValueError("DEV4.2c decision target_ref must be a nonblank string")
    if len(seen_refs) != len(set(seen_refs)):
        raise ValueError("DEV4.2c decisions must account for each observation reference once")
    if set(seen_refs) != set(observation_refs):
        raise ValueError("DEV4.2c decisions must account for exactly every DEV4.2b observation_ref")

    if result.get("next_route") != "DEV4.2d":
        raise ValueError("DEV4.2c next_route must be DEV4.2d")


def _validate_dev4_2d(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
) -> None:
    result = update["result"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]
    segment_id = update["segment_id"]

    if update["accepted_task_refs"]:
        raise ValueError("DEV4.2d requires no accepted task refs")
    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("DEV4.2d requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    prior_payload = _resolve_dev4_prior_checkpoint(
        conn,
        checkpoint_ref,
        initiative_card_id,
        initiative_id,
        segment_id,
        "DEV4.2c",
    )

    required_fields = {
        "step",
        "human_ratification_checkpoint_ref",
        "integration_records",
        "dev3_return_records",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV4.2d result must contain exact required fields")

    if result.get("human_ratification_checkpoint_ref") != checkpoint_ref:
        raise ValueError("DEV4.2d human_ratification_checkpoint_ref must name the cited checkpoint")

    decisions = prior_payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("DEV4.2c predecessor decisions must be a list")
    reject_refs = [
        decision.get("observation_ref")
        for decision in decisions
        if isinstance(decision, dict) and decision.get("outcome") == "Reject"
    ]
    integration_refs = [
        decision.get("observation_ref")
        for decision in decisions
        if isinstance(decision, dict) and decision.get("outcome") in ("Reflect", "Drop", "Defer")
    ]

    integration_records = result.get("integration_records")
    if not isinstance(integration_records, list):
        raise ValueError("DEV4.2d integration_records must be a list")
    seen_integration_refs: list[str] = []
    for record in integration_records:
        if not isinstance(record, dict):
            raise ValueError("DEV4.2d integration_records entries must be objects")
        if set(record.keys()) != {"observation_ref", "outcome", "d1_d4_result_ref"}:
            raise ValueError("DEV4.2d integration_records entries must have exactly observation_ref, outcome, and d1_d4_result_ref")
        if not _is_nonblank_str(record.get("observation_ref")):
            raise ValueError("DEV4.2d integration_records observation_ref must be a nonblank string")
        seen_integration_refs.append(record["observation_ref"])
        if record.get("outcome") not in ("Reflect", "Drop", "Defer"):
            raise ValueError("DEV4.2d integration_records outcome must be Reflect, Drop, Defer")
        if not _is_nonblank_str(record.get("d1_d4_result_ref")):
            raise ValueError("DEV4.2d integration_records d1_d4_result_ref must be a nonblank string")
    if len(seen_integration_refs) != len(set(seen_integration_refs)):
        raise ValueError("DEV4.2d integration_records must account for each observation reference once")
    if set(seen_integration_refs) != set(integration_refs):
        raise ValueError("DEV4.2d integration_records must contain exactly the Reflect/Drop/Defer decisions")

    dev3_return_records = result.get("dev3_return_records")
    if not isinstance(dev3_return_records, list):
        raise ValueError("DEV4.2d dev3_return_records must be a list")
    seen_return_refs: list[str] = []
    for record in dev3_return_records:
        if not isinstance(record, dict):
            raise ValueError("DEV4.2d dev3_return_records entries must be objects")
        if set(record.keys()) != {"observation_ref", "dev3_return_ref"}:
            raise ValueError("DEV4.2d dev3_return_records entries must have exactly observation_ref and dev3_return_ref")
        if not _is_nonblank_str(record.get("observation_ref")):
            raise ValueError("DEV4.2d dev3_return_records observation_ref must be a nonblank string")
        seen_return_refs.append(record["observation_ref"])
        if not _is_nonblank_str(record.get("dev3_return_ref")):
            raise ValueError("DEV4.2d dev3_return_records dev3_return_ref must be a nonblank string")
    if len(seen_return_refs) != len(set(seen_return_refs)):
        raise ValueError("DEV4.2d dev3_return_records must account for each observation reference once")
    if set(seen_return_refs) != set(reject_refs):
        raise ValueError("DEV4.2d dev3_return_records must contain exactly the Reject decisions")

    expected_next_route = "DEV3" if reject_refs else "DEV4.2e"
    if result.get("next_route") != expected_next_route:
        raise ValueError("DEV4.2d next_route does not match the derived route")


def _validate_dev4_2e(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
) -> None:
    result = update["result"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]
    segment_id = update["segment_id"]

    if update["accepted_task_refs"]:
        raise ValueError("DEV4.2e requires no accepted task refs")
    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("DEV4.2e requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    prior_payload = _resolve_dev4_prior_checkpoint(
        conn,
        checkpoint_ref,
        initiative_card_id,
        initiative_id,
        segment_id,
        "DEV4.2d",
    )
    if prior_payload.get("next_route") != "DEV4.2e":
        raise ValueError("DEV4.2d predecessor next_route must be DEV4.2e")

    required_fields = {
        "step",
        "disposition_checkpoint_ref",
        "implementation_context_ref",
        "segment_document_ref",
        "profile_updates",
        "all_observations_accounted",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV4.2e result must contain exact required fields")

    if result.get("disposition_checkpoint_ref") != checkpoint_ref:
        raise ValueError("DEV4.2e disposition_checkpoint_ref must name the cited checkpoint")
    if not _is_nonblank_str(result.get("implementation_context_ref")):
        raise ValueError("DEV4.2e implementation_context_ref must be a nonblank string")
    if not _is_nonblank_str(result.get("segment_document_ref")):
        raise ValueError("DEV4.2e segment_document_ref must be a nonblank string")
    profile_updates = result.get("profile_updates")
    if not isinstance(profile_updates, list):
        raise ValueError("DEV4.2e profile_updates must be a list")
    for item in profile_updates:
        if not isinstance(item, dict):
            raise ValueError("DEV4.2e profile_updates entries must be objects")
        if set(item.keys()) != {"profile", "update_ref"}:
            raise ValueError("DEV4.2e profile_updates entries must have exactly profile and update_ref")
        if not _is_nonblank_str(item.get("profile")):
            raise ValueError("DEV4.2e profile_updates profile must be a nonblank string")
        if not _is_nonblank_str(item.get("update_ref")):
            raise ValueError("DEV4.2e profile_updates update_ref must be a nonblank string")
    if result.get("all_observations_accounted") is not True:
        raise ValueError("DEV4.2e all_observations_accounted must be true")
    if result.get("next_route") != "DEV4.3":
        raise ValueError("DEV4.2e next_route must be DEV4.3")


def _resolve_dev4_prior_checkpoint_with_iteration(
    conn: Any,
    checkpoint_ref: str,
    initiative_card_id: int,
    initiative_id: str,
    segment_id: str,
    expected_step: str,
) -> tuple[dict[str, Any], int]:
    row = conn.execute(
        """
        SELECT result_id, iteration, created_at, contract_id, contract_version,
               phase, segment_id, accepted, canonical_payload
        FROM initiative_phase_results
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV4'
          AND segment_id = ?
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = ?
          AND contract_version = ?
        ORDER BY iteration DESC, created_at DESC, result_id DESC
        LIMIT 1
        """,
        (
            initiative_card_id,
            initiative_id,
            segment_id,
            _DEV4_CONTRACT_ID,
            _CONTRACT_VERSION,
        ),
    ).fetchone()
    if row is None:
        raise ValueError(f"{expected_step} prior checkpoint does not resolve to a valid DEV4 checkpoint")
    if row["result_id"] != checkpoint_ref:
        raise ValueError(f"{expected_step} checkpoint ref is not the latest accepted DEV4 checkpoint")
    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError(f"{expected_step} prior checkpoint payload is invalid")
    if not isinstance(payload, dict):
        raise ValueError(f"{expected_step} prior checkpoint payload must be an object")
    if payload.get("step") != expected_step:
        raise ValueError(f"{expected_step} prior checkpoint payload step must be {expected_step}")
    return payload, row["iteration"]


def _validate_dev4_3(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    *,
    require_effect_state: bool = True,
) -> None:
    if not isinstance(require_effect_state, bool):
        raise ValueError("DEV4.3 require_effect_state must be a bool")
    result = update["result"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]
    segment_id = update["segment_id"]

    if update["accepted_task_refs"]:
        raise ValueError("DEV4.3 requires no accepted task refs")
    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("DEV4.3 requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    _, predecessor_iteration = _resolve_dev4_prior_checkpoint_with_iteration(
        conn,
        checkpoint_ref,
        initiative_card_id,
        initiative_id,
        segment_id,
        "DEV4.2e",
    )
    if update["iteration"] != predecessor_iteration + 1:
        raise ValueError("DEV4.3 iteration must be predecessor iteration + 1")

    required_fields = {
        "step",
        "implementation_context_checkpoint_ref",
        "dev3_checkpoint_ref",
        "workspace_id",
        "member_merges",
        "merge_evidence_ref",
        "test_evidence_ref",
        "segment_boundary_evidence_ref",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV4.3 result must contain exact required fields")

    if result.get("implementation_context_checkpoint_ref") != checkpoint_ref:
        raise ValueError("DEV4.3 implementation_context_checkpoint_ref must name the cited checkpoint")

    # Resolve DEV3.9 checkpoint
    dev3_ref = result.get("dev3_checkpoint_ref")
    if not _is_nonblank_str(dev3_ref):
        raise ValueError("DEV4.3 dev3_checkpoint_ref must be a nonblank string")
    dev3_row = conn.execute(
        """
        SELECT result_id, canonical_payload
        FROM initiative_phase_results
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'DEV3'
          AND segment_id = ?
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = ?
          AND contract_version = ?
          AND result_id = ?
        """,
        (
            initiative_card_id,
            initiative_id,
            segment_id,
            _DEV3_CONTRACT_ID,
            _CONTRACT_VERSION,
            dev3_ref,
        ),
    ).fetchone()
    if dev3_row is None:
        raise ValueError("DEV4.3 dev3_checkpoint_ref does not resolve to an accepted DEV3.9 checkpoint")
    try:
        dev3_payload = json.loads(dev3_row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("DEV4.3 DEV3.9 checkpoint payload is invalid")
    if not isinstance(dev3_payload, dict):
        raise ValueError("DEV4.3 DEV3.9 checkpoint payload must be an object")
    if dev3_payload.get("step") != "DEV3.9":
        raise ValueError("DEV4.3 DEV3.9 checkpoint payload step must be DEV3.9")
    if dev3_payload.get("next_route") != "DEV4":
        raise ValueError("DEV4.3 DEV3.9 checkpoint next_route must be DEV4")

    # Workspace validation
    workspace_id = result.get("workspace_id")
    if not _is_nonblank_str(workspace_id):
        raise ValueError("DEV4.3 workspace_id must be a nonblank string")
    ws_row = conn.execute(
        """
        SELECT workspace_id, active, lifecycle_state
        FROM segment_workspaces
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND segment_id = ?
          AND active = 1
        """,
        (initiative_card_id, initiative_id, segment_id),
    ).fetchall()
    if len(ws_row) != 1:
        raise ValueError("DEV4.3 requires exactly one active workspace for this card/initiative/segment")
    if ws_row[0]["workspace_id"] != workspace_id:
        raise ValueError("DEV4.3 workspace_id must match the unique active workspace")

    # Member merges validation
    member_merges = result.get("member_merges")
    if not isinstance(member_merges, list):
        raise ValueError("DEV4.3 member_merges must be a list")

    # Get all workspace members
    members = conn.execute(
        """
        SELECT repository_identity, branch, required_base_sha, observed_head, member_state
        FROM segment_workspace_members
        WHERE workspace_id = ?
        ORDER BY repository_identity
        """,
        (workspace_id,),
    ).fetchall()
    if len(members) != len(member_merges):
        raise ValueError("DEV4.3 member_merges must cover every workspace member exactly once")

    # Validate ordering and coverage
    seen_repos: set[str] = set()
    for merge in member_merges:
        if not isinstance(merge, dict):
            raise ValueError("DEV4.3 member_merges entries must be objects")
        if set(merge.keys()) != {"repository_identity", "dev3_accepted_sha", "segment_delivery_sha", "merge_operation_id"}:
            raise ValueError("DEV4.3 member_merges entries must have exactly repository_identity, dev3_accepted_sha, segment_delivery_sha, and merge_operation_id")
        repo = merge.get("repository_identity")
        if not _is_nonblank_str(repo):
            raise ValueError("DEV4.3 member_merges repository_identity must be a nonblank string")
        if repo in seen_repos:
            raise ValueError("DEV4.3 member_merges must not duplicate repository identities")
        seen_repos.add(repo)
        accepted_sha = merge.get("dev3_accepted_sha")
        delivery_sha = merge.get("segment_delivery_sha")
        op_id = merge.get("merge_operation_id")
        if not _HEX40_RE.match(accepted_sha or ""):
            raise ValueError("DEV4.3 dev3_accepted_sha must be 40 lowercase hex characters")
        if not _HEX40_RE.match(delivery_sha or ""):
            raise ValueError("DEV4.3 segment_delivery_sha must be 40 lowercase hex characters")
        if not _is_nonblank_str(op_id):
            raise ValueError("DEV4.3 merge_operation_id must be a nonblank string")

    if seen_repos != {m["repository_identity"] for m in members}:
        raise ValueError("DEV4.3 member_merges must cover every workspace member exactly once")

    # Verify ordering by repository_identity
    merge_repos = [m["repository_identity"] for m in member_merges]
    if merge_repos != sorted(merge_repos):
        raise ValueError("DEV4.3 member_merges must be ordered by repository_identity")

    # Validate against DEV3.9 accepted_member_heads
    accepted_heads = dev3_payload.get("accepted_member_heads")
    if not isinstance(accepted_heads, list):
        raise ValueError("DEV4.3 DEV3.9 payload must contain accepted_member_heads list")
    if len(accepted_heads) != len(member_merges):
        raise ValueError("DEV4.3 accepted_member_heads count must match member_merges")
    for head, merge in zip(accepted_heads, member_merges):
        if not isinstance(head, dict):
            raise ValueError("DEV4.3 accepted_member_heads entries must be objects")
        if set(head.keys()) != {"repository_identity", "accepted_sha"}:
            raise ValueError("DEV4.3 accepted_member_heads entries must have exactly repository_identity and accepted_sha")
        if head.get("repository_identity") != merge["repository_identity"]:
            raise ValueError("DEV4.3 accepted_member_heads identity mismatch with member_merges")
        if head.get("accepted_sha") != merge["dev3_accepted_sha"]:
            raise ValueError("DEV4.3 accepted_member_heads SHA mismatch with member_merges")

    # Validate each member's state and journal
    for merge in member_merges:
        repo = merge["repository_identity"]
        delivery_sha = merge["segment_delivery_sha"]
        op_id = merge["merge_operation_id"]
        member = next((m for m in members if m["repository_identity"] == repo), None)
        if member is None:
            raise ValueError("DEV4.3 member not found in workspace")
        if require_effect_state:
            if member["member_state"] != "merged":
                raise ValueError("DEV4.3 workspace member must be merged")
            if member["observed_head"] != delivery_sha:
                raise ValueError("DEV4.3 workspace member observed_head must equal segment_delivery_sha")

            # Journal validation
            journal_rows = conn.execute(
                """
                SELECT state, operation_kind, intended_git_evidence, observed_git_evidence
                FROM external_operation_journal
                WHERE operation_id = ?
                  AND workspace_id = ?
                  AND repository_identity = ?
                  AND operation_kind = 'workspace_merge'
                ORDER BY ordinal DESC
                LIMIT 1
                """,
                (op_id, workspace_id, repo),
            ).fetchone()
            if journal_rows is None:
                raise ValueError("DEV4.3 merge operation journal entry not found")
            if journal_rows["state"] != "verified":
                raise ValueError("DEV4.3 merge operation journal must be verified")

            expected_intended = f"base={member['required_base_sha']};source={delivery_sha};branch={member['branch']}"
            if journal_rows["intended_git_evidence"] != expected_intended:
                raise ValueError("DEV4.3 merge operation intended Git evidence mismatch")

            observed = journal_rows["observed_git_evidence"]
            if not isinstance(observed, str):
                raise ValueError("DEV4.3 merge operation observed Git evidence must be present")
            obs_parts = {}
            for part in observed.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    obs_parts[k] = v
            if obs_parts.get("base") != member["required_base_sha"]:
                raise ValueError("DEV4.3 merge operation observed base mismatch")
            if obs_parts.get("source") != delivery_sha:
                raise ValueError("DEV4.3 merge operation observed source mismatch")
            merge_sha = obs_parts.get("merge", "")
            if not _HEX40_RE.match(merge_sha):
                raise ValueError("DEV4.3 merge operation observed merge must be 40hex")
            if obs_parts.get("remote") != merge_sha:
                raise ValueError("DEV4.3 merge operation observed remote must equal merge")
            if obs_parts.get("merge_commit") != "true":
                raise ValueError("DEV4.3 merge operation observed merge_commit must be true")
            if obs_parts.get("source_contained") != "true":
                raise ValueError("DEV4.3 merge operation observed source_contained must be true")
            if obs_parts.get("remote_contained") != "true":
                raise ValueError("DEV4.3 merge operation observed remote_contained must be true")

    for field in ("merge_evidence_ref", "test_evidence_ref", "segment_boundary_evidence_ref"):
        if not _is_nonblank_str(result.get(field)):
            raise ValueError(f"DEV4.3 {field} must be a nonblank string")
    if result.get("next_route") != "DEV4.4":
        raise ValueError("DEV4.3 next_route must be DEV4.4")


def _validate_dev4_4(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
    *,
    require_effect_state: bool = True,
) -> None:
    if not isinstance(require_effect_state, bool):
        raise ValueError("DEV4.4 require_effect_state must be a bool")
    result = update["result"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]
    segment_id = update["segment_id"]

    if update["accepted_task_refs"]:
        raise ValueError("DEV4.4 requires no accepted task refs")
    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("DEV4.4 requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    prior_payload, predecessor_iteration = _resolve_dev4_prior_checkpoint_with_iteration(
        conn,
        checkpoint_ref,
        initiative_card_id,
        initiative_id,
        segment_id,
        "DEV4.3",
    )
    if update["iteration"] != predecessor_iteration + 1:
        raise ValueError("DEV4.4 iteration must be predecessor iteration + 1")

    required_fields = {
        "step",
        "merge_checkpoint_ref",
        "workspace_id",
        "retirement_record_ref",
        "retired_member_ids",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV4.4 result must contain exact required fields")

    if result.get("merge_checkpoint_ref") != checkpoint_ref:
        raise ValueError("DEV4.4 merge_checkpoint_ref must name the cited checkpoint")

    workspace_id = result.get("workspace_id")
    if not _is_nonblank_str(workspace_id):
        raise ValueError("DEV4.4 workspace_id must be a nonblank string")
    if workspace_id != prior_payload.get("workspace_id"):
        raise ValueError("DEV4.4 workspace_id must match the predecessor workspace")

    # Workspace must now be inactive and retired
    ws_row = conn.execute(
        """
        SELECT workspace_id, active, lifecycle_state
        FROM segment_workspaces
        WHERE workspace_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND segment_id = ?
        """,
        (workspace_id, initiative_card_id, initiative_id, segment_id),
    ).fetchone()
    if ws_row is None:
        raise ValueError("DEV4.4 workspace not found")
    if require_effect_state:
        if ws_row["active"] != 0:
            raise ValueError("DEV4.4 workspace must be inactive")
        if ws_row["lifecycle_state"] != "retired":
            raise ValueError("DEV4.4 workspace lifecycle_state must be retired")
    else:
        if ws_row["active"] != 1:
            raise ValueError("DEV4.4 workspace must still be active")
        if ws_row["lifecycle_state"] == "retired":
            raise ValueError("DEV4.4 workspace must not yet be retired")

    # Members state
    members = conn.execute(
        """
        SELECT repository_identity, member_state
        FROM segment_workspace_members
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchall()
    if require_effect_state:
        for member in members:
            if member["member_state"] != "retired":
                raise ValueError("DEV4.4 all workspace members must be retired")
    else:
        for member in members:
            if member["member_state"] != "merged":
                raise ValueError("DEV4.4 all workspace members must be merged")

    retired_member_ids = result.get("retired_member_ids")
    if not isinstance(retired_member_ids, list):
        raise ValueError("DEV4.4 retired_member_ids must be a list")
    expected_ids = sorted(m["repository_identity"] for m in members)
    if retired_member_ids != expected_ids:
        raise ValueError("DEV4.4 retired_member_ids must be sorted, unique, and exactly every workspace repository identity")

    if not _is_nonblank_str(result.get("retirement_record_ref")):
        raise ValueError("DEV4.4 retirement_record_ref must be a nonblank string")
    if result.get("next_route") != "DEV4.5":
        raise ValueError("DEV4.4 next_route must be DEV4.5")


def _validate_dev4_5(
    conn: Any,
    update: dict[str, Any],
    initiative_card_id: int,
    initiative_id: str,
) -> None:
    result = update["result"]
    accepted_checkpoint_refs = update["accepted_checkpoint_refs"]
    segment_id = update["segment_id"]

    if update["accepted_task_refs"]:
        raise ValueError("DEV4.5 requires no accepted task refs")
    if len(accepted_checkpoint_refs) != 1:
        raise ValueError("DEV4.5 requires exactly one accepted checkpoint ref")

    checkpoint_ref = accepted_checkpoint_refs[0]
    prior_payload, predecessor_iteration = _resolve_dev4_prior_checkpoint_with_iteration(
        conn,
        checkpoint_ref,
        initiative_card_id,
        initiative_id,
        segment_id,
        "DEV4.4",
    )
    if update["iteration"] != predecessor_iteration + 1:
        raise ValueError("DEV4.5 iteration must be predecessor iteration + 1")

    required_fields = {
        "step",
        "workspace_retirement_checkpoint_ref",
        "completed_segment_id",
        "action",
        "next_segment_id",
        "closure_evidence_ref",
        "next_route",
    }
    if set(result.keys()) != required_fields:
        raise ValueError("DEV4.5 result must contain exact required fields")

    if result.get("workspace_retirement_checkpoint_ref") != checkpoint_ref:
        raise ValueError("DEV4.5 workspace_retirement_checkpoint_ref must name the cited checkpoint")
    if result.get("completed_segment_id") != segment_id:
        raise ValueError("DEV4.5 completed_segment_id must match the current segment")
    if not _is_nonblank_str(result.get("closure_evidence_ref")):
        raise ValueError("DEV4.5 closure_evidence_ref must be a nonblank string")

    # Read the latest accepted initiative segment projection
    proj_row = conn.execute(
        """
        SELECT parsed_segment_definitions
        FROM initiative_segment_projections
        WHERE initiative_card_id = ?
          AND initiative_id = ?
        ORDER BY projection_version DESC
        LIMIT 1
        """,
        (initiative_card_id, initiative_id),
    ).fetchone()
    if proj_row is None:
        raise ValueError("DEV4.5 no segment projection found")
    try:
        definitions = json.loads(proj_row["parsed_segment_definitions"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("DEV4.5 segment projection definitions are invalid")
    if not isinstance(definitions, list):
        raise ValueError("DEV4.5 segment projection definitions must be a list")

    # Order by positive unique ordinal
    ordinals: list[tuple[int, dict]] = []
    for d in definitions:
        if not isinstance(d, dict):
            raise ValueError("DEV4.5 segment definition entries must be objects")
        ordinal = d.get("ordinal")
        seg_id = d.get("segment_id")
        if not _is_positive_int(ordinal) or not _is_nonblank_str(seg_id):
            raise ValueError("DEV4.5 segment definitions require positive integer ordinal and nonblank segment_id")
        ordinals.append((ordinal, d))
    if len(ordinals) != len(set(o for o, _ in ordinals)):
        raise ValueError("DEV4.5 segment ordinals must be unique")
    ordinals.sort(key=lambda x: x[0])

    # Current segment must be declared
    current_idx = None
    for i, (_, d) in enumerate(ordinals):
        if d["segment_id"] == segment_id:
            current_idx = i
            break
    if current_idx is None:
        raise ValueError("DEV4.5 current segment is not declared in the projection")

    action = result.get("action")
    next_segment_id = result.get("next_segment_id")
    next_route = result.get("next_route")

    if current_idx < len(ordinals) - 1:
        # Next segment exists
        next_def = ordinals[current_idx + 1][1]
        next_seg_id = next_def["segment_id"]
        if action != "admit_next_segment":
            raise ValueError("DEV4.5 action must be admit_next_segment when a next segment exists")
        if next_segment_id != next_seg_id:
            raise ValueError("DEV4.5 next_segment_id must equal the immediate next ordinal segment")
        if next_route != "DEV2":
            raise ValueError("DEV4.5 next_route must be DEV2 when admitting next segment")

        # Next workspace must be planned, active, and have only planned members
        next_ws = conn.execute(
            """
            SELECT workspace_id, active, lifecycle_state
            FROM segment_workspaces
            WHERE initiative_card_id = ?
              AND initiative_id = ?
              AND segment_id = ?
            """,
            (initiative_card_id, initiative_id, next_seg_id),
        ).fetchall()
        if len(next_ws) != 1:
            raise ValueError("DEV4.5 next segment must have exactly one workspace")
        if next_ws[0]["lifecycle_state"] != "planned":
            raise ValueError("DEV4.5 next workspace must be planned")
        if next_ws[0]["active"] != 1:
            raise ValueError("DEV4.5 next workspace must be active")
        next_members = conn.execute(
            """
            SELECT member_state
            FROM segment_workspace_members
            WHERE workspace_id = ?
            """,
            (next_ws[0]["workspace_id"],),
        ).fetchall()
        for nm in next_members:
            if nm["member_state"] != "planned":
                raise ValueError("DEV4.5 next workspace members must all be planned")

        # Every declared dependency workspace must be retired/inactive
        dep_ids = next_def.get("dependency_ids", [])
        if not isinstance(dep_ids, list):
            raise ValueError("DEV4.5 dependency_ids must be a list")
        for dep_id in dep_ids:
            dep_ws = conn.execute(
                """
                SELECT active, lifecycle_state
                FROM segment_workspaces
                WHERE initiative_card_id = ?
                  AND initiative_id = ?
                  AND segment_id = ?
                """,
                (initiative_card_id, initiative_id, dep_id),
            ).fetchall()
            for dw in dep_ws:
                if dw["lifecycle_state"] != "retired" or dw["active"] != 0:
                    raise ValueError("DEV4.5 dependency workspaces must be retired and inactive")
    else:
        # Current is final
        if action != "close_initiative":
            raise ValueError("DEV4.5 action must be close_initiative when current is final")
        if next_segment_id is not None:
            raise ValueError("DEV4.5 next_segment_id must be null when closing")
        if next_route != "CLOSED":
            raise ValueError("DEV4.5 next_route must be CLOSED when closing")

        # Every projected workspace must be retired/inactive with retired members
        for _, d in ordinals:
            seg_id = d["segment_id"]
            ws_rows = conn.execute(
                """
                SELECT workspace_id, active, lifecycle_state
                FROM segment_workspaces
                WHERE initiative_card_id = ?
                  AND initiative_id = ?
                  AND segment_id = ?
                """,
                (initiative_card_id, initiative_id, seg_id),
            ).fetchall()
            for ws in ws_rows:
                if ws["lifecycle_state"] != "retired" or ws["active"] != 0:
                    raise ValueError("DEV4.5 all projected workspaces must be retired and inactive")
                ws_members = conn.execute(
                    """
                    SELECT member_state
                    FROM segment_workspace_members
                    WHERE workspace_id = ?
                    """,
                    (ws["workspace_id"],),
                ).fetchall()
                for wm in ws_members:
                    if wm["member_state"] != "retired":
                        raise ValueError("DEV4.5 all projected workspace members must be retired")


def admit_orchestration_checkpoint(
    conn: Any,
    *,
    initiative_card_id: int,
    initiative_id: str,
    actor_profile: str,
    update: dict[str, Any],
    prepared_execution: Any = None,
    approval_id: str | None = None,
    require_effect_state: bool = True,
) -> CheckpointAdmission:
    if not isinstance(require_effect_state, bool):
        raise ValueError("require_effect_state must be a bool")
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
    elif step in _DEV1_STEPS:
        expected_phase = "DEV1"
        expected_contract = _DEV1_CONTRACT_ID
    elif step in _DEV3_STEPS:
        expected_phase = "DEV3"
        expected_contract = _DEV3_CONTRACT_ID
    elif step in _DEV4_STEPS:
        expected_phase = "DEV4"
        expected_contract = _DEV4_CONTRACT_ID
    else:
        expected_phase = "DEV2"
        expected_contract = _DEV2_CONTRACT_ID

    if update["phase"] != expected_phase:
        raise ValueError("phase does not match step")
    if update["contract_id"] != expected_contract:
        raise ValueError("contract_id does not match step")
    if update["contract_version"] != _CONTRACT_VERSION:
        raise ValueError("contract_version must be 1")

    expected_segment_id = update["segment_id"]
    if step in _DEV2_STEPS or step in _DEV3_STEPS or step in _DEV4_STEPS:
        if not _is_nonblank_str(expected_segment_id):
            raise ValueError("DEV2, DEV3, and DEV4 checkpoints require a nonblank segment_id")
    else:
        if expected_segment_id is not None:
            raise ValueError("D4 and DEV1 checkpoints require null segment_id")

    _validate_latest_transition(
        conn, initiative_id, expected_phase, expected_segment_id
    )

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
        from .phase_d4_execution import admit_d4_execution

        admit_d4_execution(
            conn, initiative_card_id, initiative_id, update, prepared_execution
        )
    elif step == "DEV1.2":
        _validate_dev1_2(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )
    elif step == "DEV1.3":
        _validate_dev1_3(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )
    elif step == "DEV2.3":
        _validate_dev2_3(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )
    elif step == "DEV3.9":
        _validate_dev3_9(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )
    elif step == "DEV3.12":
        _validate_dev3_12(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )
    elif step == "DEV4.2b":
        _validate_dev4_2b(
            conn, update, initiative_card_id, initiative_id, resolved_steps
        )
    elif step == "DEV4.2c":
        _validate_dev4_2c(
            conn, update, initiative_card_id, initiative_id, approval_id
        )
    elif step == "DEV4.2d":
        _validate_dev4_2d(conn, update, initiative_card_id, initiative_id)
    elif step == "DEV4.2e":
        _validate_dev4_2e(conn, update, initiative_card_id, initiative_id)
    elif step == "DEV4.3":
        _validate_dev4_3(
            conn, update, initiative_card_id, initiative_id,
            require_effect_state=require_effect_state,
        )
    elif step == "DEV4.4":
        _validate_dev4_4(
            conn, update, initiative_card_id, initiative_id,
            require_effect_state=require_effect_state,
        )
    elif step == "DEV4.5":
        _validate_dev4_5(conn, update, initiative_card_id, initiative_id)

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

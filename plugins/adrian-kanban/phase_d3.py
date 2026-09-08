"""Phase D3 fixed result preparation for Adrian Kanban."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .phase_d1 import _nonblank, _validate_record_list
from .phase_d2_evidence import load_d2_history, load_d2_review
from .published_artifact import VerifiedArtifact, verify_published_artifact


@dataclass(frozen=True)
class PreparedD3Result:
    initiative_id: str
    update_digest: str
    reviewed: VerifiedArtifact
    decision_record: VerifiedArtifact


def prepare_d3_result(
    initiative_id: object, update: object, read_published_blob, read_git_blob
) -> PreparedD3Result:
    _nonblank(initiative_id, "initiative_id")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")
    if update.get("phase") != "D3":
        raise ValueError("update.phase must be 'D3'")
    if update.get("segment_id") is not None:
        raise ValueError("update.segment_id must be None")
    if update.get("result_kind") != "phase_close":
        raise ValueError("update.result_kind must be 'phase_close'")
    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")
    expected_keys = {
        "reviewed_ref",
        "decision_record_ref",
        "d2_result_ref",
        "finding_coverage",
        "decision_items",
        "next_route",
    }
    if set(result.keys()) != expected_keys:
        raise ValueError(
            "update.result must have exactly keys reviewed_ref, decision_record_ref, d2_result_ref, finding_coverage, decision_items, next_route"
        )
    _nonblank(result["d2_result_ref"], "update.result.d2_result_ref")
    _validate_record_list(
        result["finding_coverage"],
        "update.result.finding_coverage",
        ("finding_ref", "status", "rationale"),
        "finding_ref",
    )
    allowed = ("decision_required", "no_decision_required")
    for i, rec in enumerate(result["finding_coverage"]):
        if rec["status"] not in allowed:
            raise ValueError(
                f"finding_coverage[{i}]: invalid status; allowed values: {allowed}"
            )
    decision_items = result["decision_items"]
    if not isinstance(decision_items, list):
        raise ValueError("update.result.decision_items must be a list")
    seen_refs: set[str] = set()
    for i, rec in enumerate(decision_items):
        if not isinstance(rec, dict):
            raise ValueError(f"update.result.decision_items[{i}] must be a dict")
        if set(rec.keys()) != {
            "finding_ref",
            "disposition",
            "rationale",
            "amendment_text",
            "canon_decision",
        }:
            raise ValueError(
                f"update.result.decision_items[{i}] must have exactly keys finding_ref, disposition, rationale, amendment_text, canon_decision"
            )
        _nonblank(rec["finding_ref"], f"update.result.decision_items[{i}].finding_ref")
        _nonblank(rec["rationale"], f"update.result.decision_items[{i}].rationale")
        if rec["finding_ref"] in seen_refs:
            raise ValueError("update.result.decision_items has duplicate finding_ref")
        seen_refs.add(rec["finding_ref"])
        disposition = rec["disposition"]
        if disposition not in ("ratified_as_is", "design_amendment", "canon_amendment"):
            raise ValueError(
                f"update.result.decision_items[{i}].disposition must be one of ratified_as_is, design_amendment, canon_amendment"
            )
        amendment_text = rec["amendment_text"]
        canon_decision = rec["canon_decision"]
        if disposition == "design_amendment":
            _nonblank(
                amendment_text, f"update.result.decision_items[{i}].amendment_text"
            )
            if canon_decision is not None:
                raise ValueError(
                    f"update.result.decision_items[{i}].canon_decision must be None for design_amendment"
                )
        elif disposition == "canon_amendment":
            _nonblank(
                canon_decision, f"update.result.decision_items[{i}].canon_decision"
            )
            if amendment_text is not None:
                raise ValueError(
                    f"update.result.decision_items[{i}].amendment_text must be None for canon_amendment"
                )
        else:
            if amendment_text is not None:
                raise ValueError(
                    f"update.result.decision_items[{i}].amendment_text must be None for ratified_as_is"
                )
            if canon_decision is not None:
                raise ValueError(
                    f"update.result.decision_items[{i}].canon_decision must be None for ratified_as_is"
                )
    required_refs = {
        row["finding_ref"]
        for row in result["finding_coverage"]
        if row["status"] == "decision_required"
    }
    decision_refs = {rec["finding_ref"] for rec in decision_items}
    if decision_refs != required_refs:
        raise ValueError(
            "decision_items finding_ref set must exactly match decision_required finding_coverage refs"
        )
    has_design_amendment = any(
        rec["disposition"] == "design_amendment" for rec in decision_items
    )
    expected_route = "D2" if has_design_amendment else "D4"
    if result["next_route"] != expected_route:
        raise ValueError(f"update.result.next_route must be '{expected_route}'")
    reviewed = verify_published_artifact(result["reviewed_ref"], read_published_blob)
    decision_record = verify_published_artifact(
        result["decision_record_ref"], read_git_blob
    )
    canonical = json.dumps(
        update,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return PreparedD3Result(
        initiative_id=initiative_id,
        update_digest=digest,
        reviewed=reviewed,
        decision_record=decision_record,
    )


def admit_d3_result(context, initiative_card_id, initiative_id, update, prepared):
    conn = context.connection
    if type(prepared) is not PreparedD3Result:
        raise ValueError("prepared must be a PreparedD3Result")
    _nonblank(initiative_id, "initiative_id")
    if prepared.initiative_id != initiative_id:
        raise ValueError("prepared.initiative_id does not match initiative_id")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")
    canonical = json.dumps(
        update,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if prepared.update_digest != digest:
        raise ValueError("update digest mismatch")
    row = conn.execute(
        """
        SELECT result_id, canonical_payload, contract_id, contract_version
        FROM initiative_phase_results
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'D2'
          AND segment_id IS NULL
          AND result_kind = 'phase_close'
          AND accepted = 1
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (initiative_card_id, initiative_id),
    ).fetchone()
    if row is None:
        raise ValueError("no accepted D2 phase_close result found")
    if row["contract_id"] != "adrian-kanban.lifecycle.d2":
        raise ValueError("D2 result contract_id mismatch")
    if row["contract_version"] != "1":
        raise ValueError("D2 result contract_version mismatch")
    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")
    if result.get("d2_result_ref") != row["result_id"]:
        raise ValueError("result.d2_result_ref does not match D2 result_id")
    try:
        stored = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("D2 canonical_payload is invalid JSON") from None
    if not isinstance(stored, dict):
        raise ValueError("D2 canonical_payload is not an object")
    if stored.get("conclusion") != "DRY":
        raise ValueError("D2 conclusion is not DRY")
    if stored.get("next_route") != "D3":
        raise ValueError("D2 next_route is not D3")
    expected_draft = asdict(prepared.reviewed)
    if stored.get("draft_ref") != expected_draft:
        raise ValueError("stored draft_ref does not match prepared.reviewed")
    dispositions = stored.get("finding_dispositions")
    if not isinstance(dispositions, list):
        raise ValueError("stored finding_dispositions must be a list")
    stored_refs = set()
    for d in dispositions:
        if not isinstance(d, dict):
            raise ValueError("finding_disposition entry must be a dict")
        ref = d.get("finding_ref")
        _nonblank(ref, "finding_ref")
        if ref in stored_refs:
            raise ValueError("duplicate finding_ref in finding_dispositions")
        stored_refs.add(ref)
    coverage = result.get("finding_coverage")
    if not isinstance(coverage, list):
        raise ValueError("result.finding_coverage must be a list")
    coverage_refs = {rec["finding_ref"] for rec in coverage}
    if coverage_refs != stored_refs:
        raise ValueError("result.finding_coverage refs do not match stored refs")
    history = load_d2_history(conn, initiative_card_id, initiative_id)
    history_refs = set()
    for review in history:
        for ref in review.finding_refs:
            history_refs.add(ref)
    if history_refs != stored_refs:
        raise ValueError("history finding refs do not match stored refs")
    accepted_task_refs = update.get("accepted_task_refs")
    if not isinstance(accepted_task_refs, list):
        raise ValueError("update.accepted_task_refs must be a list")
    task_ref_set = set()
    for ref in accepted_task_refs:
        _nonblank(ref, "accepted_task_refs entry")
        if ref in task_ref_set:
            raise ValueError("duplicate entry in accepted_task_refs")
        task_ref_set.add(ref)
    candidate_refs = {review.candidate_ref for review in history}
    if task_ref_set != candidate_refs:
        raise ValueError("accepted_task_refs do not match history candidate refs")
    if update.get("accepted_checkpoint_refs") != []:
        raise ValueError("update.accepted_checkpoint_refs must be empty list")
    current_ref = stored.get("current_review_ref")
    _nonblank(current_ref, "current_review_ref")
    load_d2_review(
        conn, initiative_card_id, initiative_id, current_ref, prepared.reviewed
    )
    return None

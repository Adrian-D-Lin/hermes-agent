"""Phase D2 fixed result preparation and locked admission for Adrian Kanban."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .phase_d1 import _nonblank, _validate_record_list
from .published_artifact import VerifiedArtifact, verify_published_artifact
from .phase_d2_evidence import load_d2_review, load_d2_history


@dataclass(frozen=True)
class PreparedD2Result:
    initiative_id: str
    update_digest: str
    draft: VerifiedArtifact
    review: VerifiedArtifact


def prepare_d2_result(
    initiative_id: object,
    update: object,
    read_published_blob,
    read_git_blob,
) -> PreparedD2Result:
    _nonblank(initiative_id, "initiative_id")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")
    if update.get("phase") != "D2":
        raise ValueError("update.phase must be 'D2'")
    if update.get("segment_id") is not None:
        raise ValueError("update.segment_id must be None")
    if update.get("result_kind") != "phase_close":
        raise ValueError("update.result_kind must be 'phase_close'")

    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")
    expected_keys = {
        "draft_ref",
        "review_ref",
        "current_review_ref",
        "prior_d2_result_ref",
        "finding_dispositions",
        "conclusion",
        "next_route",
    }
    if set(result.keys()) != expected_keys:
        raise ValueError(
            "update.result must have exactly keys "
            "draft_ref, review_ref, current_review_ref, prior_d2_result_ref, "
            "finding_dispositions, conclusion, next_route"
        )

    _nonblank(result["current_review_ref"], "update.result.current_review_ref")
    prior_ref = result["prior_d2_result_ref"]
    if prior_ref is not None:
        _nonblank(prior_ref, "update.result.prior_d2_result_ref")

    _validate_record_list(
        result["finding_dispositions"],
        "update.result.finding_dispositions",
        ("finding_ref", "classification", "disposition", "rationale"),
        "finding_ref",
    )

    classifications = [rec["classification"] for rec in result["finding_dispositions"]]
    valid_classifications = {"novel_material", "coverage", "review_process_failure"}
    for c in classifications:
        if c not in valid_classifications:
            raise ValueError(
                "finding_dispositions.classification must be novel_material, coverage, or review_process_failure; use an allowed classification and retry"
            )

    if "review_process_failure" in classifications:
        raise ValueError(
            "review_process_failure requires remediation recover/re-dispatch independent review before closing D2"
        )

    conclusion = result["conclusion"]
    next_route = result["next_route"]
    if conclusion == "DRY":
        if next_route != "D3":
            raise ValueError("conclusion DRY requires next_route D3")
        if "novel_material" in classifications:
            raise ValueError("conclusion DRY requires no novel_material findings")
    elif conclusion == "NOT_DRY":
        if next_route != "D1":
            raise ValueError("conclusion NOT_DRY requires next_route D1")
        if "novel_material" not in classifications:
            raise ValueError(
                "conclusion NOT_DRY requires at least one novel_material finding"
            )
    else:
        raise ValueError("conclusion must be exactly DRY or NOT_DRY")

    draft = verify_published_artifact(result["draft_ref"], read_published_blob)
    review = verify_published_artifact(result["review_ref"], read_git_blob)

    canonical = json.dumps(
        update,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()

    return PreparedD2Result(
        initiative_id=initiative_id,
        update_digest=digest,
        draft=draft,
        review=review,
    )


def admit_d2_result(
    context,
    initiative_card_id: int,
    initiative_id: object,
    update: object,
    prepared: object,
) -> None:
    if type(prepared) is not PreparedD2Result:
        raise ValueError("prepared must be a PreparedD2Result instance")
    _nonblank(initiative_id, "initiative_id")
    if prepared.initiative_id != initiative_id:
        raise ValueError("initiative_id does not match prepared result")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")

    canonical = json.dumps(
        update,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != prepared.update_digest:
        raise ValueError("update digest does not match prepared result")

    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")

    accepted_task_refs = update.get("accepted_task_refs")
    if not isinstance(accepted_task_refs, list) or not accepted_task_refs:
        raise ValueError("accepted_task_refs must be a nonempty list")
    seen_tasks: set[str] = set()
    for i, ref in enumerate(accepted_task_refs):
        _nonblank(ref, f"accepted_task_refs[{i}]")
        if ref in seen_tasks:
            raise ValueError("accepted_task_refs has duplicate entry")
        seen_tasks.add(ref)

    accepted_checkpoint_refs = update.get("accepted_checkpoint_refs")
    if accepted_checkpoint_refs != []:
        raise ValueError("accepted_checkpoint_refs must be empty")

    conn = context.connection
    history = load_d2_history(conn, initiative_card_id, initiative_id)
    history_refs = set(review.candidate_ref for review in history)
    if seen_tasks != history_refs:
        raise ValueError(
            "accepted_task_refs must exactly match all historical D2 review candidate_refs"
        )

    current_review_ref = result["current_review_ref"]
    if current_review_ref not in seen_tasks:
        raise ValueError("current_review_ref must be in accepted_task_refs")

    load_d2_review(
        conn, initiative_card_id, initiative_id, current_review_ref, prepared.draft
    )

    all_finding_refs: set[str] = set()
    for review in history:
        all_finding_refs.update(review.finding_refs)

    disposition_refs: set[str] = set()
    for rec in result["finding_dispositions"]:
        disposition_refs.add(rec["finding_ref"])

    if all_finding_refs != disposition_refs:
        raise ValueError(
            "finding_dispositions finding_ref set must exactly match all historical findings"
        )

    row = conn.execute(
        """
        SELECT result_id, contract_id, contract_version
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

    prior_ref = result["prior_d2_result_ref"]
    if row is None:
        if prior_ref is not None:
            raise ValueError(
                "prior_d2_result_ref must be None when no prior D2 result exists"
            )
    else:
        if (
            row["contract_id"] != "adrian-kanban.lifecycle.d2"
            or row["contract_version"] != "1"
        ):
            raise ValueError("prior D2 result has unexpected contract")
        if prior_ref != row["result_id"]:
            raise ValueError(
                "prior_d2_result_ref does not match latest accepted D2 result"
            )

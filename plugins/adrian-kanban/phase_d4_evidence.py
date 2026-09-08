from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .initiative_checkpoints import _resolve_task_ref
from .lifecycle import LifecycleContractRepository
from .output_validators import validate_lifecycle_output
from .published_artifact import VerifiedArtifact, verify_published_artifact
from .segment_manifest import (
    _validate_commit,
    _validate_manifest_path,
    _validate_nonblank_str,
)


@dataclass(frozen=True)
class AcceptedD4Evidence:
    candidate_ref: str
    step: str
    task_card_id: int
    task_id: str
    metadata: dict
    item_refs: tuple[str, ...]


def load_d4_evidence(
    conn,
    initiative_card_id: int,
    initiative_id: str,
    candidate_ref: str,
    expected_step: str,
) -> AcceptedD4Evidence:
    if expected_step not in {"D4.1", "D4.2", "D4.5"}:
        raise ValueError("expected_step must be D4.1, D4.2, or D4.5")
    profile, validator = {
        "D4.1": ("independent-reviewer", "d4_1_edit_set_v1"),
        "D4.2": ("test-authority-reviewer", "d4_2_verification_v1"),
        "D4.5": ("test-authority-reviewer", "d4_5_post_write_v1"),
    }[expected_step]
    step, task_card_id, task_id = _resolve_task_ref(
        conn, candidate_ref, initiative_card_id, initiative_id
    )
    if step != expected_step:
        raise ValueError("candidate ref does not resolve to the expected step")
    record = LifecycleContractRepository(conn).load(task_id)
    if record is None:
        raise ValueError("no lifecycle contract record for task")
    if record.initiative_card_id != initiative_card_id:
        raise ValueError("lifecycle contract initiative card mismatch")
    if record.snapshot.initiative_id != initiative_id:
        raise ValueError("lifecycle contract initiative mismatch")
    if record.snapshot.phase != "D4":
        raise ValueError("lifecycle contract phase is not D4")
    if record.snapshot.step != expected_step:
        raise ValueError("lifecycle contract step is not the expected step")
    if record.snapshot.execution_profile != profile:
        raise ValueError("lifecycle contract execution profile mismatch")
    handoff = conn.execute(
        """
        SELECT metadata_json
        FROM task_candidate_handoffs
        WHERE candidate_id = ?
          AND task_card_id = ?
          AND task_id = ?
        """,
        (candidate_ref, task_card_id, task_id),
    ).fetchone()
    if handoff is None:
        raise ValueError("no candidate handoff found for the reviewed candidate")
    try:
        metadata = json.loads(handoff["metadata_json"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("candidate handoff metadata is invalid") from None
    validate_lifecycle_output(validator, metadata)
    if expected_step == "D4.2" and metadata["item_count"] != len(
        metadata["edit_findings"]
    ):
        raise ValueError(
            f"item_count ({metadata['item_count']}) must equal the number of edit_findings "
            f"({len(metadata['edit_findings'])}); retry."
        )
    if expected_step == "D4.1":
        item_refs = tuple(
            f"{candidate_ref}#/edit_set/{i}" for i in range(len(metadata["edit_set"]))
        )
    elif expected_step == "D4.2":
        item_refs = tuple(
            f"{candidate_ref}#/edit_findings/{i}"
            for i in range(len(metadata["edit_findings"]))
        )
    else:
        item_refs = ()
    return AcceptedD4Evidence(
        candidate_ref=candidate_ref,
        step=expected_step,
        task_card_id=task_card_id,
        task_id=task_id,
        metadata=metadata,
        item_refs=item_refs,
    )


def validate_d4_source_pair(
    conn,
    initiative_card_id: int,
    initiative_id: str,
    author_ref: str,
    verifier_ref: str,
    approved_change_set_digest: str,
) -> tuple[AcceptedD4Evidence, AcceptedD4Evidence]:
    author = load_d4_evidence(
        conn, initiative_card_id, initiative_id, author_ref, "D4.1"
    )
    verifier = load_d4_evidence(
        conn, initiative_card_id, initiative_id, verifier_ref, "D4.2"
    )
    verifier_record = LifecycleContractRepository(conn).load(verifier.task_id)
    if verifier_record is None:
        raise ValueError("no lifecycle contract record for verifier task")
    if verifier_record.snapshot.predecessor_ref != author_ref:
        raise ValueError("verifier snapshot predecessor_ref does not match author_ref")
    if author_ref not in verifier_record.snapshot.prior_record_refs:
        raise ValueError("author_ref not found in verifier snapshot prior_record_refs")
    author_item_refs = set(author.item_refs)
    verifier_edit_refs = set()
    for row in verifier.metadata["edit_findings"]:
        edit_ref = row["edit_ref"]
        if edit_ref not in author_item_refs:
            raise ValueError(
                f"verifier edit_ref {edit_ref!r} does not reference an author item_ref"
            )
        verifier_edit_refs.add(edit_ref)
    if verifier_edit_refs != author_item_refs:
        raise ValueError("verifier edit_findings do not cover all author item_refs")
    if not isinstance(approved_change_set_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", approved_change_set_digest
    ):
        raise ValueError(
            "approved_change_set_digest must be a 64-character lowercase hex string"
        )
    canonical_json = json.dumps(
        author.metadata["edit_set"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    computed_digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    if computed_digest != approved_change_set_digest:
        raise ValueError(
            f"approved_change_set_digest mismatch: expected {computed_digest}, got {approved_change_set_digest}"
        )
    return author, verifier


@dataclass(frozen=True)
class PreparedD4Result:
    initiative_id: str
    update_digest: str
    baseline: VerifiedArtifact


def prepare_d4_result(initiative_id, update, read_published_blob) -> "PreparedD4Result":
    _validate_nonblank_str(initiative_id, "initiative_id")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")
    if update.get("phase") != "D4":
        raise ValueError("update.phase must be 'D4'")
    if update.get("segment_id") is not None:
        raise ValueError("update.segment_id must be None")
    if update.get("result_kind") != "phase_close":
        raise ValueError("update.result_kind must be 'phase_close'")
    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")
    if set(result.keys()) != {"verified_baseline_ref", "verification_record_ref", "next_route"}:
        raise ValueError(
            "update.result must have exactly keys verified_baseline_ref, verification_record_ref, next_route"
        )
    if result["next_route"] != "DEV1":
        raise ValueError("update.result.next_route must be 'DEV1'")
    verification_record_ref = _validate_nonblank_str(
        result["verification_record_ref"], "update.result.verification_record_ref"
    )
    baseline = verify_published_artifact(result["verified_baseline_ref"], read_published_blob)
    accepted_task_refs = update.get("accepted_task_refs")
    if type(accepted_task_refs) is not list or accepted_task_refs != [verification_record_ref]:
        raise ValueError(
            "update.accepted_task_refs must equal [verification_record_ref]"
        )
    accepted_checkpoint_refs = update.get("accepted_checkpoint_refs")
    if (
        type(accepted_checkpoint_refs) is not list
        or len(accepted_checkpoint_refs) != 1
        or not isinstance(accepted_checkpoint_refs[0], str)
        or not accepted_checkpoint_refs[0].strip()
    ):
        raise ValueError(
            "update.accepted_checkpoint_refs must be exactly one nonblank D4.4 record ID"
        )
    digest = hashlib.sha256(
        json.dumps(
            update,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return PreparedD4Result(
        initiative_id=initiative_id,
        update_digest=digest,
        baseline=baseline,
    )


def admit_d4_result(context, initiative_card_id, initiative_id, update, prepared) -> None:
    """Read-only admission validation for a D4 phase_close result.

    No mutations, no lease re-query. Returns None on success.
    """
    from .phase_d4_execution import _validate_determinations

    conn = context.connection
    if type(prepared) is not PreparedD4Result:
        raise ValueError("prepared must be a PreparedD4Result")
    _validate_nonblank_str(initiative_id, "initiative_id")
    if prepared.initiative_id != initiative_id:
        raise ValueError("prepared.initiative_id does not match initiative_id")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")
    digest = hashlib.sha256(
        json.dumps(
            update,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if digest != prepared.update_digest:
        raise ValueError("update digest mismatch; the proof was prepared against a different update")
    if type(prepared.baseline) is not VerifiedArtifact:
        raise ValueError("prepared.baseline must be a VerifiedArtifact")
    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")
    if set(result.keys()) != {"verified_baseline_ref", "verification_record_ref", "next_route"}:
        raise ValueError(
            "update.result must have exactly keys verified_baseline_ref, verification_record_ref, next_route"
        )
    if result["next_route"] != "DEV1":
        raise ValueError("update.result.next_route must be 'DEV1'")
    verification_record_ref = _validate_nonblank_str(
        result["verification_record_ref"], "update.result.verification_record_ref"
    )
    if result["verified_baseline_ref"] != {
        "path": prepared.baseline.path,
        "commit": prepared.baseline.commit,
        "sha256": prepared.baseline.sha256,
    }:
        raise ValueError("update.result.verified_baseline_ref does not match the prepared baseline")

    # accepted_task_refs must equal [verification_record_ref]
    accepted_task_refs = update.get("accepted_task_refs")
    if type(accepted_task_refs) is not list or accepted_task_refs != [verification_record_ref]:
        raise ValueError("update.accepted_task_refs must equal [verification_record_ref]")

    # accepted_checkpoint_refs: exactly one nonblank D4.4 record ID
    accepted_checkpoint_refs = update.get("accepted_checkpoint_refs")
    if (
        type(accepted_checkpoint_refs) is not list
        or len(accepted_checkpoint_refs) != 1
        or not isinstance(accepted_checkpoint_refs[0], str)
        or not accepted_checkpoint_refs[0].strip()
    ):
        raise ValueError(
            "update.accepted_checkpoint_refs must be exactly one nonblank D4.4 record ID"
        )
    d44_ref = accepted_checkpoint_refs[0]

    # Load the referenced accepted D4.4 checkpoint
    row = conn.execute(
        """
        SELECT canonical_payload, accepted_task_refs, accepted_checkpoint_refs
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'D4'
          AND segment_id IS NULL
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = 'adrian-kanban.lifecycle.d4'
          AND contract_version = '1'
        """,
        (d44_ref, initiative_card_id, initiative_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "accepted_checkpoint_refs[0] does not resolve to an accepted D4.4 "
            "orchestration checkpoint; verify the checkpoint was admitted and "
            "its identity fields match this initiative"
        )

    try:
        d44_payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("D4.4 checkpoint canonical_payload is malformed JSON") from None
    if not isinstance(d44_payload, dict):
        raise ValueError("D4.4 checkpoint canonical_payload must be the RESULT object")

    # D4.4 payload step must be D4.4
    if d44_payload.get("step") != "D4.4":
        raise ValueError("D4.4 checkpoint payload.step must be 'D4.4'")

    # Parse D4.4 accepted_checkpoint_refs to find the D4.3 ref
    try:
        d44_accepted_checkpoints = json.loads(row["accepted_checkpoint_refs"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("D4.4 checkpoint accepted_checkpoint_refs is malformed JSON") from None
    if (
        type(d44_accepted_checkpoints) is not list
        or len(d44_accepted_checkpoints) != 1
        or not isinstance(d44_accepted_checkpoints[0], str)
        or not d44_accepted_checkpoints[0].strip()
    ):
        raise ValueError(
            "D4.4 checkpoint accepted_checkpoint_refs must be exactly one nonblank D4.3 record ID"
        )
    d43_ref = d44_accepted_checkpoints[0]

    # Load the D4.3 checkpoint to get its accepted_task_refs (D4.1 + D4.2)
    d43_row = conn.execute(
        """
        SELECT canonical_payload, accepted_task_refs
        FROM initiative_phase_results
        WHERE result_id = ?
          AND initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'D4'
          AND segment_id IS NULL
          AND result_kind = 'orchestration_checkpoint'
          AND accepted = 1
          AND contract_id = 'adrian-kanban.lifecycle.d4'
          AND contract_version = '1'
        """,
        (d43_ref, initiative_card_id, initiative_id),
    ).fetchone()
    if d43_row is None:
        raise ValueError(
            "D4.3 checkpoint referenced by D4.4 does not resolve to an accepted "
            "orchestration checkpoint; verify the D4.3 write-gate approval was admitted"
        )

    try:
        d43_payload = json.loads(d43_row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("D4.3 checkpoint canonical_payload is malformed JSON") from None
    if not isinstance(d43_payload, dict):
        raise ValueError("D4.3 checkpoint canonical_payload must be the RESULT object")
    if d43_payload.get("step") != "D4.3":
        raise ValueError("D4.3 checkpoint payload.step must be 'D4.3'")

    # D4.3/D4.4 approval and changeset digest must match
    d43_approval = d43_payload.get("write_gate_approval_ref")
    d44_approval = d44_payload.get("write_gate_approval_ref")
    if not isinstance(d43_approval, str) or not d43_approval.strip():
        raise ValueError("D4.3 checkpoint payload requires a nonblank write_gate_approval_ref")
    if not isinstance(d44_approval, str) or not d44_approval.strip():
        raise ValueError("D4.4 checkpoint payload requires a nonblank write_gate_approval_ref")
    if d43_approval != d44_approval:
        raise ValueError("D4.3 and D4.4 write_gate_approval_ref do not match")
    d43_digest = d43_payload.get("approved_change_set_digest")
    d44_digest = d44_payload.get("approved_change_set_digest")
    if not isinstance(d43_digest, str) or not d43_digest.strip():
        raise ValueError("D4.3 checkpoint payload requires a nonblank approved_change_set_digest")
    if not isinstance(d44_digest, str) or not d44_digest.strip():
        raise ValueError("D4.4 checkpoint payload requires a nonblank approved_change_set_digest")
    if d43_digest != d44_digest:
        raise ValueError("D4.3 and D4.4 approved_change_set_digest do not match")

    # Parse D4.3 accepted_task_refs (exactly two: one D4.1, one D4.2)
    try:
        d43_accepted_tasks = json.loads(d43_row["accepted_task_refs"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("D4.3 checkpoint accepted_task_refs is malformed JSON") from None
    if not isinstance(d43_accepted_tasks, list) or len(d43_accepted_tasks) != 2:
        raise ValueError(
            "D4.3 checkpoint accepted_task_refs must contain exactly two refs "
            "(one D4.1 author and one D4.2 verifier)"
        )
    for ref in d43_accepted_tasks:
        if type(ref) is not str or not ref.strip():
            raise ValueError("D4.3 checkpoint accepted_task_refs must contain nonblank strings")
    if len(set(d43_accepted_tasks)) != len(d43_accepted_tasks):
        raise ValueError("D4.3 checkpoint accepted_task_refs must be distinct")

    # Resolve each task ref to its lifecycle step
    resolved_steps = {}
    for ref in d43_accepted_tasks:
        step, _, _ = _resolve_task_ref(conn, ref, initiative_card_id, initiative_id)
        resolved_steps[ref] = step
    steps = sorted(resolved_steps.values())
    if steps != ["D4.1", "D4.2"]:
        raise ValueError(
            "D4.3 checkpoint accepted_task_refs must resolve to exactly one D4.1 and one D4.2 step"
        )
    author_ref = next(ref for ref in d43_accepted_tasks if resolved_steps[ref] == "D4.1")
    verifier_ref = next(ref for ref in d43_accepted_tasks if resolved_steps[ref] == "D4.2")

    # Bind the complete review pair to the exact approved change set
    author, verifier = validate_d4_source_pair(
        conn,
        initiative_card_id,
        initiative_id,
        author_ref,
        verifier_ref,
        d44_payload["approved_change_set_digest"],
    )

    # Determinations must cover the union of author and verifier item refs
    determinations = _validate_determinations(
        d44_payload.get("item_determinations"), "D4.4.item_determinations"
    )
    expected_item_ids = set(author.item_refs) | set(verifier.item_refs)
    actual_item_ids = {det["item_id"] for det in determinations}
    if actual_item_ids != expected_item_ids:
        missing = expected_item_ids - actual_item_ids
        invented = actual_item_ids - expected_item_ids
        detail = []
        if missing:
            detail.append(f"missing={sorted(missing)}")
        if invented:
            detail.append(f"invented={sorted(invented)}")
        raise ValueError(
            "D4.4 item_determinations item_ids must exactly equal the "
            "union of author and verifier item_refs; " + "; ".join(detail)
        )

    # Load D4.5 evidence
    d45 = load_d4_evidence(conn, initiative_card_id, initiative_id, verification_record_ref, "D4.5")
    snapshot = LifecycleContractRepository(conn).load(d45.task_id).snapshot

    # Lifecycle snapshot predecessor_ref must match the D4.4 ID
    if snapshot.predecessor_ref != d44_ref:
        raise ValueError(
            "D4.5 lifecycle snapshot predecessor_ref does not match the D4.4 checkpoint ID"
        )
    # prior_record_refs must contain both D4.3 and D4.4 IDs
    if d43_ref not in snapshot.prior_record_refs:
        raise ValueError(
            "D4.5 lifecycle snapshot prior_record_refs does not contain the D4.3 checkpoint ID"
        )
    if d44_ref not in snapshot.prior_record_refs:
        raise ValueError(
            "D4.5 lifecycle snapshot prior_record_refs does not contain the D4.4 checkpoint ID"
        )

    metadata = d45.metadata

    # Validate document_verifications
    verifications = metadata["document_verifications"]
    seen_paths: set[str] = set()
    for entry in verifications:
        path = _validate_manifest_path(entry["path"])
        sha = _validate_commit(entry["sha"], "document_verifications.sha")
        if path in seen_paths:
            raise ValueError("document_verifications has duplicate document paths")
        seen_paths.add(path)
        if entry["result"] != "MATCH":
            raise ValueError(
                f"document_verifications result for {path!r} is {entry['result']!r}; "
                "every verification must be exactly MATCH"
            )

    # Verification path/sha set must exactly equal D4.4 post_write_documents
    d44_post_docs = d44_payload.get("post_write_documents")
    if not isinstance(d44_post_docs, list) or not d44_post_docs:
        raise ValueError("D4.4 post_write_documents must be a nonempty list")
    seen_d44_paths: set[str] = set()
    for doc in d44_post_docs:
        p = _validate_manifest_path(doc["path"])
        _validate_commit(doc["sha"], "D4.4.post_write_documents.sha")
        if p in seen_d44_paths:
            raise ValueError("D4.4 post_write_documents has duplicate document paths")
        seen_d44_paths.add(p)
    d44_doc_set = {(doc["path"], doc["sha"]) for doc in d44_post_docs}
    d45_doc_set = {(v["path"], v["sha"]) for v in verifications}
    if d45_doc_set != d44_doc_set:
        raise ValueError(
            "D4.5 document_verifications path/sha set does not exactly match D4.4 post_write_documents"
        )

    # Document paths must equal source edit_set paths
    source_edit_paths = {_validate_manifest_path(edit["document_ref"]) for edit in author.metadata["edit_set"]}
    if seen_paths != source_edit_paths:
        raise ValueError(
            "D4.5 document paths do not exactly match the source edit_set document_ref paths"
        )

    # source_item_count and determination_count each equal the derived original item total
    original_item_total = len(expected_item_ids)
    if metadata["source_item_count"] != original_item_total:
        raise ValueError(
            f"D4.5 source_item_count ({metadata['source_item_count']}) must equal the "
            f"derived original item total ({original_item_total})"
        )
    if metadata["determination_count"] != original_item_total:
        raise ValueError(
            f"D4.5 determination_count ({metadata['determination_count']}) must equal the "
            f"derived original item total ({original_item_total})"
        )

    # development_baseline_ref must equal prepared.baseline.path + '@' + prepared.baseline.commit
    expected_baseline_ref = prepared.baseline.path + "@" + prepared.baseline.commit
    if metadata["development_baseline_ref"] != expected_baseline_ref:
        raise ValueError(
            "D4.5 development_baseline_ref does not match the prepared baseline reference"
        )
    # baseline must be in D4.5 snapshot.baseline_refs
    if expected_baseline_ref not in snapshot.baseline_refs:
        raise ValueError(
            "D4.5 lifecycle snapshot baseline_refs does not contain the development baseline reference"
        )

    return None

"""Phase D4 execution preparation for Adrian Kanban.

Preparation only. No registry/database mutations, no binding/registry
getter, no Git implementation, no review semantical NLP.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .phase_d4_lease import VerifiedExecutionLease, verify_execution_lease
from .published_artifact import VerifiedArtifact
from .segment_manifest import (
    _validate_commit,
    _validate_manifest_path,
    _validate_nonblank_str,
    _validate_sha256,
    _validate_strict_dict,
)

_EXECUTION_RESULT_KEYS = {
    "step",
    "write_gate_approval_ref",
    "approval_lease_ref",
    "approved_change_set_digest",
    "execution_result",
    "post_write_documents",
    "item_determinations",
    "execution_started_at",
    "execution_finished_at",
}

_ITEM_DETERMINATION_VALUES = {"applied", "deferred", "rejected"}


def _validate_determinations(value, field: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a nonempty list")
    optional_keys = {"deferral_route", "rejection_reason"}
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{field} must be a dict")
        unknown = set(item.keys()) - ({"item_id", "determination"} | optional_keys)
        if unknown:
            raise ValueError(f"{field} has unknown fields: {sorted(unknown)}")
        missing = {"item_id", "determination"} - set(item.keys())
        if missing:
            raise ValueError(f"{field} is missing required fields: {sorted(missing)}")
        item_id = _validate_nonblank_str(item["item_id"], f"{field}.item_id")
        determination = _validate_nonblank_str(
            item["determination"], f"{field}.determination"
        )
        if determination not in _ITEM_DETERMINATION_VALUES:
            raise ValueError(
                f"{field}.determination must be one of "
                f"{sorted(_ITEM_DETERMINATION_VALUES)}"
            )
        if "deferral_route" in item:
            _validate_nonblank_str(item["deferral_route"], f"{field}.deferral_route")
        if "rejection_reason" in item:
            _validate_nonblank_str(item["rejection_reason"], f"{field}.rejection_reason")
        if determination == "deferred":
            if "deferral_route" not in item:
                raise ValueError(
                    f"{field}.deferral_route is required when determination is deferred"
                )
        elif determination == "rejected":
            if "rejection_reason" not in item:
                raise ValueError(
                    f"{field}.rejection_reason is required when determination is rejected"
                )
        if item_id in seen_ids:
            raise ValueError(f"{field} item_id must be unique")
        seen_ids.add(item_id)
    return value


def prepare_d4_execution(
    initiative_id,
    update,
    registry,
    worktree_path,
    read_immutable_blob,
) -> "PreparedD4Execution":
    if not isinstance(initiative_id, str) or not initiative_id.strip():
        raise ValueError("initiative_id must be a nonblank string")

    if not isinstance(update, dict):
        raise ValueError("update must be a dict")
    if update["phase"] != "D4":
        raise ValueError("update.phase must be 'D4'")
    if update["segment_id"] is not None:
        raise ValueError("update.segment_id must be None")

    result = _validate_strict_dict(
        update["result"], "update.result", _EXECUTION_RESULT_KEYS
    )

    step = _validate_nonblank_str(result["step"], "update.result.step")
    if step != "D4.4":
        raise ValueError("update.result.step must be 'D4.4'")

    _validate_nonblank_str(
        result["write_gate_approval_ref"], "update.result.write_gate_approval_ref"
    )
    approval_lease_ref = _validate_nonblank_str(
        result["approval_lease_ref"], "update.result.approval_lease_ref"
    )
    _validate_sha256(
        result["approved_change_set_digest"], "update.result.approved_change_set_digest"
    )
    _validate_nonblank_str(result["execution_result"], "update.result.execution_result")

    post_write_documents = result["post_write_documents"]
    if not isinstance(post_write_documents, list) or not post_write_documents:
        raise ValueError("update.result.post_write_documents must be a nonempty list")
    seen_paths: set[str] = set()
    post_refs = []
    for doc in post_write_documents:
        _validate_strict_dict(
            doc, "update.result.post_write_documents", {"path", "sha"}
        )
        path = _validate_manifest_path(doc["path"])
        sha = _validate_commit(doc["sha"], "update.result.post_write_documents.sha")
        if path in seen_paths:
            raise ValueError("update.result.post_write_documents path must be unique")
        seen_paths.add(path)
        post_refs.append({"path": path, "sha": sha})

    _validate_determinations(
        result["item_determinations"], "update.result.item_determinations"
    )

    execution_started_at = _validate_nonblank_str(
        result["execution_started_at"], "update.result.execution_started_at"
    )
    execution_finished_at = _validate_nonblank_str(
        result["execution_finished_at"], "update.result.execution_finished_at"
    )

    try:
        lease = registry.get_lease(approval_lease_ref)
    except Exception:
        raise ValueError(
            "Unable to load Write-Gate authorization records; verify registry "
            "availability and retry."
        ) from None
    if lease is None:
        raise ValueError("approval lease not found")

    approval_id = _validate_nonblank_str(
        result["write_gate_approval_ref"], "update.result.write_gate_approval_ref"
    )
    paths = [ref["path"] for ref in post_refs]

    lease_proof = verify_execution_lease(
        registry,
        approval_id=approval_id,
        lease_id=approval_lease_ref,
        session_id=lease.session_id,
        worktree_path=worktree_path,
        document_paths=paths,
        execution_started_at=execution_started_at,
        execution_finished_at=execution_finished_at,
    )

    documents: list[VerifiedArtifact] = []
    for ref in post_refs:
        blob = read_immutable_blob(ref["sha"], ref["path"])
        if not isinstance(blob, bytes):
            raise ValueError("read_immutable_blob must return bytes")
        documents.append(
            VerifiedArtifact(
                path=ref["path"],
                commit=ref["sha"],
                sha256=hashlib.sha256(blob).hexdigest(),
            )
        )

    content_digest = hashlib.sha256(
        json.dumps(
            update,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    return PreparedD4Execution(
        initiative_id=initiative_id,
        update_digest=content_digest,
        lease=lease_proof,
        documents=tuple(documents),
    )


def validate_execution_proof(initiative_id, update, prepared) -> None:
    if type(prepared) is not PreparedD4Execution:
        raise ValueError("prepared must be a PreparedD4Execution")
    if prepared.initiative_id != initiative_id:
        raise ValueError("prepared.initiative_id mismatch")
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
        raise ValueError("update digest mismatch")
    result = update["result"]
    if type(prepared.lease) is not VerifiedExecutionLease:
        raise ValueError("prepared.lease must be a VerifiedExecutionLease")
    if prepared.lease.approval_id != result["write_gate_approval_ref"]:
        raise ValueError("lease.approval_id mismatch")
    if prepared.lease.lease_id != result["approval_lease_ref"]:
        raise ValueError("lease.lease_id mismatch")
    if prepared.lease.execution_started_at != result["execution_started_at"]:
        raise ValueError("lease.execution_started_at mismatch")
    if prepared.lease.execution_finished_at != result["execution_finished_at"]:
        raise ValueError("lease.execution_finished_at mismatch")
    if not isinstance(prepared.documents, tuple):
        raise ValueError("prepared.documents must be a tuple")
    for document in prepared.documents:
        if type(document) is not VerifiedArtifact:
            raise ValueError("prepared.documents entries must be VerifiedArtifact")
    if tuple((d.path, d.commit) for d in prepared.documents) != tuple(
        (r["path"], r["sha"]) for r in result["post_write_documents"]
    ):
        raise ValueError("documents do not match post_write_documents")
    return None


@dataclass(frozen=True)
class PreparedD4Execution:
    initiative_id: str
    update_digest: str
    lease: "VerifiedExecutionLease"
    documents: tuple["VerifiedArtifact", ...]


def admit_d4_execution(conn, initiative_card_id, initiative_id, update, prepared) -> None:
    """Admission-only validation for a D4 execution result.

    Read-only helper. No mutations, no new schema. The next task supplies the
    command-path integration.
    """
    from .initiative_checkpoints import _resolve_task_ref
    from .phase_d4_evidence import validate_d4_source_pair

    # 1. Validate the prepared proof against the exact update envelope.
    validate_execution_proof(initiative_id, update, prepared)

    if not isinstance(update, dict):
        raise ValueError("update must be a dict")

    # 2. accepted_checkpoint_refs must be exactly one nonblank string.
    checkpoint_refs = update.get("accepted_checkpoint_refs")
    if type(checkpoint_refs) is not list or len(checkpoint_refs) != 1:
        raise ValueError(
            "accepted_checkpoint_refs must be a list of exactly one entry; "
            "supply the single accepted D4.3 orchestration checkpoint ref"
        )
    checkpoint_ref = checkpoint_refs[0]
    if type(checkpoint_ref) is not str or not checkpoint_ref.strip():
        raise ValueError(
            "accepted_checkpoint_refs[0] must be a nonblank string; supply the "
            "accepted D4.3 checkpoint result_id"
        )

    row = conn.execute(
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
        (checkpoint_ref, initiative_card_id, initiative_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            "accepted_checkpoint_refs[0] does not resolve to an accepted D4.3 "
            "orchestration checkpoint; verify the checkpoint was admitted and "
            "its identity fields match this initiative"
        )

    # 3. Decode both JSON columns safely.
    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(
            "D4.3 checkpoint canonical_payload is malformed JSON; re-admit the "
            "checkpoint with valid canonical payload"
        ) from None
    if not isinstance(payload, dict):
        raise ValueError(
            "D4.3 checkpoint canonical_payload must be the RESULT object"
        )
    try:
        accepted_task_refs = json.loads(row["accepted_task_refs"])
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(
            "D4.3 checkpoint accepted_task_refs is malformed JSON; re-admit the "
            "checkpoint with valid accepted_task_refs"
        ) from None
    if not isinstance(accepted_task_refs, list):
        raise ValueError(
            "D4.3 checkpoint accepted_task_refs must be a list"
        )

    result = update["result"]
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")

    # Payload step must be D4.3 and its approval/digest must equal the current
    # update result values.
    if payload.get("step") != "D4.3":
        raise ValueError(
            "D4.3 checkpoint payload.step must be 'D4.3'; the referenced "
            "checkpoint is not a write-gate approval step"
        )
    payload_approval = payload.get("write_gate_approval_ref")
    if not isinstance(payload_approval, str) or not payload_approval.strip():
        raise ValueError(
            "D4.3 checkpoint payload requires a nonblank write_gate_approval_ref"
        )
    payload_digest = payload.get("approved_change_set_digest")
    if not isinstance(payload_digest, str) or not payload_digest:
        raise ValueError(
            "D4.3 checkpoint payload requires a nonblank approved_change_set_digest"
        )
    if result.get("write_gate_approval_ref") != payload_approval:
        raise ValueError(
            "update.result.write_gate_approval_ref does not match the D4.3 "
            "checkpoint write_gate_approval_ref"
        )
    if result.get("approved_change_set_digest") != payload_digest:
        raise ValueError(
            "update.result.approved_change_set_digest does not match the D4.3 "
            "checkpoint approved_change_set_digest"
        )

    # Exactly two distinct nonblank accepted task refs.
    if len(accepted_task_refs) != 2:
        raise ValueError(
            "D4.3 checkpoint accepted_task_refs must contain exactly two refs; "
            "the write-gate approval requires one D4.1 author and one D4.2 verifier"
        )
    for ref in accepted_task_refs:
        if type(ref) is not str or not ref.strip():
            raise ValueError(
                "D4.3 checkpoint accepted_task_refs must contain nonblank strings"
            )
    if len(set(accepted_task_refs)) != len(accepted_task_refs):
        raise ValueError(
            "D4.3 checkpoint accepted_task_refs must be distinct"
        )

    # 4. Resolve each task ref to its lifecycle step.
    resolved_steps = {}
    for ref in accepted_task_refs:
        step, _, _ = _resolve_task_ref(
            conn, ref, initiative_card_id, initiative_id
        )
        resolved_steps[ref] = step
    steps = sorted(resolved_steps.values())
    if steps != ["D4.1", "D4.2"]:
        raise ValueError(
            "D4.3 checkpoint accepted_task_refs must resolve to exactly one "
            "D4.1 and one D4.2 step"
        )
    author_ref = next(ref for ref in accepted_task_refs if resolved_steps[ref] == "D4.1")
    verifier_ref = next(ref for ref in accepted_task_refs if resolved_steps[ref] == "D4.2")

    # Bind the complete review pair to the exact approved change set.
    author, verifier = validate_d4_source_pair(
        conn,
        initiative_card_id,
        initiative_id,
        author_ref,
        verifier_ref,
        result["approved_change_set_digest"],
    )

    # 5. Determinations must cover the union of author and verifier item refs.
    determinations = _validate_determinations(
        result.get("item_determinations"), "update.result.item_determinations"
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
            "update.result.item_determinations item_ids must exactly equal the "
            "union of author and verifier item_refs; " + "; ".join(detail)
        )

    # 6. Expected document paths are the validated document_ref values from the
    # author edit_set; they must exactly match the prepared document paths.
    expected_doc_paths = {
        _validate_manifest_path(edit["document_ref"])
        for edit in author.metadata["edit_set"]
    }
    prepared_doc_paths = {doc.path for doc in prepared.documents}
    if prepared_doc_paths != expected_doc_paths:
        missing = expected_doc_paths - prepared_doc_paths
        extra = prepared_doc_paths - expected_doc_paths
        detail = []
        if missing:
            detail.append(f"missing={sorted(missing)}")
        if extra:
            detail.append(f"extra={sorted(extra)}")
        raise ValueError(
            "prepared document paths must exactly match the author edit_set "
            "document_ref values; " + "; ".join(detail)
        )
    return None

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .initiative_checkpoints import _resolve_task_ref
from .lifecycle import LifecycleContractRepository
from .output_validators import validate_lifecycle_output


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

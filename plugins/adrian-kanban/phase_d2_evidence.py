from __future__ import annotations

import json
from dataclasses import dataclass

from .lifecycle import LifecycleContractRecord, LifecycleContractRepository
from .published_artifact import VerifiedArtifact
from .initiative_checkpoints import _resolve_task_ref
from .output_validators import validate_lifecycle_output


@dataclass(frozen=True)
class AcceptedD2Review:
    candidate_ref: str
    finding_refs: tuple[str, ...]
    conclusion: str


def _load_candidate(
    conn,
    initiative_card_id: int,
    initiative_id: str,
    candidate_ref: str,
) -> tuple[LifecycleContractRecord, int, str, AcceptedD2Review]:
    step, task_card_id, task_id = _resolve_task_ref(
        conn, candidate_ref, initiative_card_id, initiative_id
    )
    if step != "D2":
        raise ValueError("candidate ref does not resolve to a D2 step")

    record = LifecycleContractRepository(conn).load(task_id)
    if record is None:
        raise ValueError("no lifecycle contract record for task")
    if record.initiative_card_id != initiative_card_id:
        raise ValueError("lifecycle contract initiative card mismatch")
    if record.snapshot.initiative_id != initiative_id:
        raise ValueError("lifecycle contract initiative mismatch")
    if record.snapshot.phase != "D2":
        raise ValueError("lifecycle contract phase is not D2")
    if record.snapshot.step != "D2":
        raise ValueError("lifecycle contract step is not D2")
    if record.snapshot.execution_profile != "independent-reviewer":
        raise ValueError(
            "lifecycle contract execution profile is not independent-reviewer"
        )

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
    except (json.JSONDecodeError, TypeError):
        raise ValueError("candidate handoff metadata is invalid") from None

    validate_lifecycle_output("d2_review_v1", metadata)

    finding_refs = tuple(
        f"{candidate_ref}#/findings/{i}" for i in range(len(metadata["findings"]))
    )

    review = AcceptedD2Review(
        candidate_ref=candidate_ref,
        finding_refs=finding_refs,
        conclusion=metadata["conclusion"],
    )
    return record, task_card_id, task_id, review


def load_d2_review(
    conn,
    initiative_card_id: int,
    initiative_id: str,
    candidate_ref: str,
    draft: VerifiedArtifact,
) -> AcceptedD2Review:
    if type(draft) is not VerifiedArtifact:
        raise ValueError("draft must be a VerifiedArtifact")

    record, task_card_id, task_id, review = _load_candidate(
        conn, initiative_card_id, initiative_id, candidate_ref
    )

    if draft.path not in record.snapshot.baseline_refs:
        raise ValueError(
            "draft path is not designated as a baseline in the lifecycle snapshot"
        )

    rows = conn.execute(
        """
        SELECT 1
        FROM task_input_entries e
        JOIN task_input_manifests m
          ON m.task_card_id = e.task_card_id
         AND m.task_id = e.task_id
        WHERE e.task_card_id = ?
          AND e.task_id = ?
          AND e.workspace_path = ?
          AND e.sha256 = ?
          AND e.source_kind = 'git_commit'
          AND e.source_locator = ?
          AND m.declared_inputs_accessible = 1
        """,
        (task_card_id, task_id, draft.path, draft.sha256, draft.commit),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            "expected exactly one accessible input entry for the reviewed artifact"
        )

    return review


def load_d2_history(
    conn,
    initiative_card_id: int,
    initiative_id: str,
) -> tuple[AcceptedD2Review, ...]:
    rows = conn.execute(
        """
        SELECT DISTINCT h.candidate_id
        FROM task_candidate_handoffs h
        JOIN task_reviewer_verdicts v
          ON h.task_card_id = v.task_card_id
         AND h.task_id = v.task_id
         AND h.candidate_id = v.candidate_id
        JOIN task_lifecycle_contracts c
          ON h.task_card_id = c.task_card_id
         AND h.task_id = c.task_id
        WHERE c.initiative_card_id = ?
          AND c.initiative_id = ?
          AND c.step = 'D2'
          AND v.verdict = 'accepted'
        ORDER BY h.candidate_id
        """,
        (initiative_card_id, initiative_id),
    ).fetchall()

    return tuple(
        _load_candidate(conn, initiative_card_id, initiative_id, r["candidate_id"])[3]
        for r in rows
    )

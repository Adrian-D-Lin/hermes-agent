"""Segment-manifest projection admission and persistence for Adrian Kanban."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .segment_manifest import PreparedSegmentManifest, _percent_encode

_CONTRACT_ID = "adrian-kanban.lifecycle.dev1"
_CONTRACT_VERSION = "1"
_WORKSPACE_CONTROLLER = "adrian-kanban:workspace-controller:v1"
_LIFECYCLE_STATE = "planned"
_MEMBER_STATE = "planned"


@dataclass(frozen=True)
class WorkspaceRow:
    workspace_id: str
    initiative_card_id: int
    initiative_id: str
    segment_id: str
    projection_id: str
    lifecycle_state: str
    controller_binding_ref: str
    active: int


@dataclass(frozen=True)
class MemberRow:
    workspace_id: str
    repository_identity: str
    relative_path: str
    branch: str
    required_base_sha: str | None
    observed_head: str | None
    member_state: str


@dataclass(frozen=True)
class SegmentProjectionAdmission:
    result_id: str
    projection_id: str
    projection_version: int
    initiative_card_id: int
    initiative_id: str
    manifest_path: str
    manifest_sha: str
    content_digest: str
    parsed_segment_definitions: str
    readiness_refs: str
    accepted_task_refs: tuple[str, ...]
    checkpoint_ref: str
    workspaces: tuple[WorkspaceRow, ...]
    members: tuple[MemberRow, ...]


def _validate_nonblank(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a normalized nonblank string")
    return value


def _validate_positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive int")
    return value


def _validate_actor_evidence(value: dict, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a dict")
    if value.get("actor_profile") != "default":
        raise ValueError(f"{field}.actor_profile must be default")
    return value


def _validate_canonical_payload(value: dict, field: str, step: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a dict")
    if value.get("step") != step:
        raise ValueError(f"{field}.step must be {step}")
    return value


def _resolve_accepted_candidate(
    conn: sqlite3.Connection,
    *,
    initiative_card_id: int,
    initiative_id: str,
    board: str,
    step: str,
) -> tuple[str, str]:
    rows = conn.execute(
        "SELECT c.task_id, h.candidate_id "
        "FROM task_lifecycle_contracts lc "
        "JOIN adrian_kanban_cards c ON c.id = lc.task_card_id "
        "AND c.task_id = lc.task_id "
        "JOIN task_candidate_handoffs h ON h.task_card_id = c.id "
        "AND h.task_id = c.task_id "
        "JOIN task_reviewer_verdicts v ON v.task_card_id = c.id "
        "AND v.task_id = c.task_id AND v.candidate_id = h.candidate_id "
        "WHERE lc.initiative_card_id = ? AND lc.initiative_id = ? "
        "AND c.initiative_id = ? AND c.board_slug = ? "
        "AND c.card_type = 'task' AND lc.step = ? "
        "AND lc.contract_id = ? AND lc.contract_version = ? "
        "AND v.verdict = 'accepted'",
        (
            initiative_card_id,
            initiative_id,
            initiative_id,
            board,
            step,
            _CONTRACT_ID,
            _CONTRACT_VERSION,
        ),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"expected exactly one accepted candidate for {step}")
    task_id, candidate_id = rows[0]
    _validate_nonblank(task_id, f"task_id for {step}")
    _validate_nonblank(candidate_id, f"candidate_id for {step}")
    return task_id, candidate_id


def admit_segment_projection(
    conn: sqlite3.Connection,
    *,
    initiative_card_id: int,
    initiative_id: str,
    board: str,
    actor_profile: str,
    prepared: PreparedSegmentManifest,
) -> SegmentProjectionAdmission:
    if type(conn) is not sqlite3.Connection:
        raise ValueError("conn must be a sqlite3.Connection")
    _validate_positive_int(initiative_card_id, "initiative_card_id")
    _validate_nonblank(initiative_id, "initiative_id")
    _validate_nonblank(board, "board")
    if actor_profile != "default":
        raise ValueError("actor_profile must be default")
    if type(prepared) is not PreparedSegmentManifest:
        raise ValueError("prepared must be a PreparedSegmentManifest")
    if prepared.initiative_id != initiative_id:
        raise ValueError("initiative mismatch")

    transition = conn.execute(
        "SELECT to_phase, to_segment_id FROM initiative_transitions "
        "WHERE initiative_card_id = ? AND initiative_id = ? "
        "ORDER BY transition_id DESC LIMIT 1",
        (initiative_card_id, initiative_id),
    ).fetchone()
    if transition is None:
        raise ValueError("no transition found")
    if transition[0] != "DEV1" or transition[1] is not None:
        raise ValueError("latest transition must be DEV1/null segment")

    checkpoint = conn.execute(
        "SELECT result_id, contract_id, contract_version, actor_evidence, "
        "canonical_payload FROM initiative_phase_results "
        "WHERE initiative_card_id = ? AND initiative_id = ? "
        "AND phase = 'DEV1' AND segment_id IS NULL "
        "AND result_kind = 'orchestration_checkpoint' AND accepted = 1 "
        "ORDER BY iteration DESC, created_at DESC, result_id DESC LIMIT 1",
        (initiative_card_id, initiative_id),
    ).fetchone()
    if checkpoint is None:
        raise ValueError("no accepted checkpoint found")
    (
        checkpoint_result_id,
        contract_id,
        contract_version,
        actor_evidence_json,
        payload_json,
    ) = checkpoint
    _validate_nonblank(checkpoint_result_id, "checkpoint result_id")
    if contract_id != _CONTRACT_ID or contract_version != _CONTRACT_VERSION:
        raise ValueError("checkpoint contract mismatch")
    _validate_actor_evidence(json.loads(actor_evidence_json), "actor_evidence")
    _validate_canonical_payload(
        json.loads(payload_json), "canonical_payload", "DEV1.3"
    )

    source_task_ids = (
        prepared.scope_ref_kanban_card,
        prepared.segmentation_ref_kanban_card,
        prepared.segment_review_ref_kanban_card,
    )
    task_refs = []
    for step, expected_task_id in zip(
        ("DEV1.4", "DEV1.5", "DEV1.6"), source_task_ids, strict=True
    ):
        task_id, candidate_id = _resolve_accepted_candidate(
            conn,
            initiative_card_id=initiative_card_id,
            initiative_id=initiative_id,
            board=board,
            step=step,
        )
        if task_id != expected_task_id:
            raise ValueError(f"manifest task ID mismatch for {step}")
        task_refs.append(candidate_id)
    if len(set(task_refs)) != len(task_refs):
        raise ValueError("accepted candidate IDs must be unique")

    if conn.execute(
        "SELECT 1 FROM initiative_segment_projections WHERE projection_id = ?",
        (prepared.projection_id,),
    ).fetchone():
        raise ValueError("projection_id already exists")
    if conn.execute(
        "SELECT 1 FROM initiative_phase_results WHERE result_id = ?",
        (prepared.result_id,),
    ).fetchone():
        raise ValueError("result_id already exists")
    if conn.execute(
        "SELECT 1 FROM segment_workspaces WHERE initiative_card_id = ? "
        "AND active = 1",
        (initiative_card_id,),
    ).fetchone():
        raise ValueError("active segment workspace exists")

    version_row = conn.execute(
        "SELECT COALESCE(MAX(projection_version), 0) + 1 "
        "FROM initiative_segment_projections WHERE initiative_card_id = ?",
        (initiative_card_id,),
    ).fetchone()
    projection_version = version_row[0]
    _validate_positive_int(projection_version, "projection_version")

    readiness_map = json.loads(prepared.readiness_refs)
    if not isinstance(readiness_map, dict):
        raise ValueError("readiness_refs must parse to a dict")
    segment_ids = [segment.segment_id for segment in prepared.segments]
    if set(readiness_map) != set(segment_ids):
        raise ValueError("readiness_refs keys must equal segment IDs")
    for value in readiness_map.values():
        _validate_nonblank(value, "readiness_ref")
    if len(set(readiness_map.values())) != len(readiness_map):
        raise ValueError("readiness_refs values must be unique")

    workspaces = []
    members = []
    seen_workspace_ids = set()
    seen_repo_pairs = set()
    seen_relative_paths = set()
    seen_repo_branches = set()
    for segment in prepared.segments:
        workspace_id = _validate_nonblank(segment.workspace_id, "workspace_id")
        if workspace_id in seen_workspace_ids:
            raise ValueError(f"duplicate workspace_id {workspace_id}")
        seen_workspace_ids.add(workspace_id)
        workspaces.append(
            WorkspaceRow(
                workspace_id=workspace_id,
                initiative_card_id=initiative_card_id,
                initiative_id=initiative_id,
                segment_id=segment.segment_id,
                projection_id=prepared.projection_id,
                lifecycle_state=_LIFECYCLE_STATE,
                controller_binding_ref=_WORKSPACE_CONTROLLER,
                active=1,
            )
        )
        branch = (
            f"{_percent_encode(initiative_id)}/"
            f"{_percent_encode(segment.segment_id)}"
        )
        for repository_identity in segment.repository_members:
            pair = (workspace_id, repository_identity)
            if pair in seen_repo_pairs:
                raise ValueError(f"duplicate workspace/repository pair {pair}")
            seen_repo_pairs.add(pair)
            relative_path = (
                f"{_percent_encode(initiative_id)}/"
                f"{_percent_encode(segment.segment_id)}/"
                f"{_percent_encode(repository_identity)}"
            )
            if relative_path in seen_relative_paths:
                raise ValueError(f"duplicate relative_path {relative_path}")
            seen_relative_paths.add(relative_path)
            repo_branch = (repository_identity, branch)
            if repo_branch in seen_repo_branches:
                raise ValueError(f"duplicate repository/branch tuple {repo_branch}")
            seen_repo_branches.add(repo_branch)
            members.append(
                MemberRow(
                    workspace_id=workspace_id,
                    repository_identity=repository_identity,
                    relative_path=relative_path,
                    branch=branch,
                    required_base_sha=None,
                    observed_head=None,
                    member_state=_MEMBER_STATE,
                )
            )

    return SegmentProjectionAdmission(
        result_id=prepared.result_id,
        projection_id=prepared.projection_id,
        projection_version=projection_version,
        initiative_card_id=initiative_card_id,
        initiative_id=initiative_id,
        manifest_path=prepared.manifest_path,
        manifest_sha=prepared.manifest_sha,
        content_digest=prepared.content_digest,
        parsed_segment_definitions=prepared.parsed_segment_definitions,
        readiness_refs=prepared.readiness_refs,
        accepted_task_refs=tuple(task_refs),
        checkpoint_ref=checkpoint_result_id,
        workspaces=tuple(workspaces),
        members=tuple(members),
    )


def persist_segment_projection(
    conn: sqlite3.Connection,
    admission: SegmentProjectionAdmission,
    *,
    actor_evidence: dict,
    idempotency_key: str,
    created_at: int,
) -> None:
    if not conn.in_transaction:
        raise ValueError("must be in a transaction")
    if type(admission) is not SegmentProjectionAdmission:
        raise ValueError("admission must be a SegmentProjectionAdmission")
    _validate_actor_evidence(actor_evidence, "actor_evidence")
    _validate_nonblank(idempotency_key, "idempotency_key")
    _validate_positive_int(created_at, "created_at")

    payload = {
        "content_digest": admission.content_digest,
        "manifest_path": admission.manifest_path,
        "manifest_sha": admission.manifest_sha,
        "projection_id": admission.projection_id,
        "projection_version": admission.projection_version,
        "readiness_refs": json.loads(admission.readiness_refs),
    }
    canonical_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    conn.execute(
        "INSERT INTO initiative_phase_results "
        "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
        "iteration, result_kind, contract_id, contract_version, "
        "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
        "actor_evidence, idempotency_key, accepted, created_at) VALUES "
        "(?, ?, ?, 'DEV1', NULL, 1, 'segment_manifest_projection', ?, ?, ?, "
        "?, ?, ?, ?, 1, ?)",
        (
            admission.result_id,
            admission.initiative_card_id,
            admission.initiative_id,
            _CONTRACT_ID,
            _CONTRACT_VERSION,
            canonical_payload,
            json.dumps(
                list(admission.accepted_task_refs),
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                [admission.checkpoint_ref],
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(actor_evidence, sort_keys=True, separators=(",", ":")),
            idempotency_key,
            created_at,
        ),
    )
    conn.execute(
        "INSERT INTO initiative_segment_projections "
        "(projection_id, projection_version, initiative_card_id, initiative_id, "
        "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
        "readiness_refs, validation_result, projected_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)",
        (
            admission.projection_id,
            admission.projection_version,
            admission.initiative_card_id,
            admission.initiative_id,
            admission.manifest_path,
            admission.manifest_sha,
            admission.content_digest,
            admission.parsed_segment_definitions,
            admission.readiness_refs,
            created_at,
        ),
    )
    for workspace in admission.workspaces:
        conn.execute(
            "INSERT INTO segment_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, segment_id, "
            "projection_id, lifecycle_state, controller_binding_ref, active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace.workspace_id,
                workspace.initiative_card_id,
                workspace.initiative_id,
                workspace.segment_id,
                workspace.projection_id,
                workspace.lifecycle_state,
                workspace.controller_binding_ref,
                workspace.active,
                created_at,
                created_at,
            ),
        )
    for member in admission.members:
        conn.execute(
            "INSERT INTO segment_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                member.workspace_id,
                member.repository_identity,
                member.relative_path,
                member.branch,
                member.required_base_sha,
                member.observed_head,
                member.member_state,
                created_at,
            ),
        )

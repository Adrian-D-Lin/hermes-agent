"""Controlled purge-and-replace admission and finalization for adrian-kanban."""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, FrozenSet

from .handoffs import canonical_handoff_requirements, normalize_handoff_requirements
from .lifecycle import LifecycleContractRepository, LifecycleRecordRejected


@dataclass(frozen=True)
class PurgeReplacementAdmission:
    replacement_id: str
    initiative_card_id: int
    initiative_id: str
    board: str
    actor_profile: str
    session_id: str
    approval_id: str
    predecessor_task_card_id: int
    predecessor_task_id: str
    predecessor_record_version: int
    eligibility_classification: str
    eligibility_evidence_ref: Any
    repository_worktree_disposition: dict
    expected_relations: dict
    successor_payload: dict
    handoff_governed: bool
    creation_defects: tuple
    lifecycle_association: dict | None
    predecessor_workspace_kind: str | None
    predecessor_workspace_path: str | None
    predecessor_branch_name: str | None
    predecessor_project_id: str | None
    predecessor_attachment_paths: tuple


def _is_nonblank_str(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool) and value > 0


def _is_non_negative_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool) and value >= 0


def _normalize_str(value: Any) -> str:
    if not _is_nonblank_str(value):
        raise ValueError("value must be a nonblank string")
    return value.strip()


def _validate_known_profiles(known_profiles: Any) -> FrozenSet[str]:
    if type(known_profiles) not in {tuple, frozenset, set}:
        raise ValueError("known_profiles must be a tuple, frozenset, or set")
    profiles = []
    for profile in known_profiles:
        if not _is_nonblank_str(profile):
            raise ValueError("known_profiles must contain only nonblank strings")
        profiles.append(profile.strip())
    if len(profiles) != len(set(profiles)):
        raise ValueError("known_profiles must contain unique values")
    if not profiles:
        raise ValueError("known_profiles must be nonempty")
    return frozenset(profiles)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_initiative_card(
    conn: sqlite3.Connection, initiative_card_id: int, initiative_id: str, board: str
) -> dict:
    cur = conn.execute(
        """
        SELECT id, initiative_id, board_slug, record_version
        FROM adrian_kanban_cards
        WHERE id = ? AND card_type = 'initiative' AND task_id IS NULL
        """,
        (initiative_card_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError("initiative card not found")
    if row[1] != initiative_id:
        raise ValueError("initiative card identity mismatch")
    if row[2] != board:
        raise ValueError("initiative card board mismatch")
    return {
        "id": row[0],
        "initiative_id": row[1],
        "board_slug": row[2],
        "record_version": row[3],
    }


def _load_predecessor_task(
    conn: sqlite3.Connection,
    task_id: str,
    initiative_id: str,
    board: str,
    expected_version: int,
) -> dict:
    cur = conn.execute(
        """
        SELECT t.id, t.task_id, t.initiative_id, t.board_slug, t.record_version,
               n.workspace_kind, n.workspace_path, n.branch_name, n.project_id,
               n.status, n.current_run_id, n.claim_lock, n.claim_expires, n.worker_pid,
               n.consecutive_failures, n.goal_mode, n.assignee
        FROM adrian_kanban_cards t
        JOIN tasks n ON n.id = t.task_id
        WHERE t.card_type = 'task' AND t.task_id = ? AND t.initiative_id = ? AND t.board_slug = ?
        """,
        (task_id, initiative_id, board),
    )
    rows = cur.fetchall()
    if len(rows) != 1:
        raise ValueError("predecessor task resolution failed")
    row = rows[0]
    if row[4] != expected_version:
        raise ValueError("predecessor record version mismatch")
    return {
        "card_id": row[0],
        "task_id": row[1],
        "initiative_id": row[2],
        "board_slug": row[3],
        "record_version": row[4],
        "workspace_kind": row[5],
        "workspace_path": row[6],
        "branch_name": row[7],
        "project_id": row[8],
        "status": row[9],
        "current_run_id": row[10],
        "claim_lock": row[11],
        "claim_expires": row[12],
        "worker_pid": row[13],
        "consecutive_failures": row[14],
        "goal_mode": row[15],
        "assignee": row[16],
    }


def _check_active_execution(conn: sqlite3.Connection, task_id: str, task: dict) -> None:
    if task["status"] in ("running", "review"):
        raise ValueError("predecessor has active execution status")
    if (
        task["current_run_id"] is not None
        or task["claim_lock"] is not None
        or task["claim_expires"] is not None
        or task["worker_pid"] is not None
    ):
        raise ValueError("predecessor has active execution fields")
    cur = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND status = 'running' AND ended_at IS NULL",
        (task_id,),
    )
    if cur.fetchone() is not None:
        raise ValueError("predecessor has active running task_run")


def _check_accepted_verdict(
    conn: sqlite3.Connection, task_card_id: int, task_id: str
) -> None:
    cur = conn.execute(
        "SELECT 1 FROM task_reviewer_verdicts WHERE task_card_id = ? AND task_id = ? AND verdict = 'accepted'",
        (task_card_id, task_id),
    )
    if cur.fetchone() is not None:
        raise ValueError("predecessor has accepted reviewer verdict")


def _check_accepted_phase_refs(
    conn: sqlite3.Connection, initiative_card_id: int, task_id: str
) -> None:
    cur = conn.execute(
        "SELECT candidate_id FROM task_candidate_handoffs WHERE task_id = ?",
        (task_id,),
    )
    protected = set()
    for row in cur.fetchall():
        protected.add(row[0])
    protected.add(task_id)

    cur = conn.execute(
        "SELECT accepted_task_refs FROM initiative_phase_results WHERE accepted = 1"
    )
    for row in cur.fetchall():
        refs_raw = row[0]
        if refs_raw is None:
            continue
        try:
            refs = json.loads(refs_raw)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("invalid accepted_task_refs JSON")
        if type(refs) is not list:
            raise ValueError("accepted_task_refs must be a list")
        for ref in refs:
            if not _is_nonblank_str(ref):
                raise ValueError("accepted_task_refs must contain nonblank strings")
            if ref in protected:
                raise ValueError("predecessor task_id is in accepted phase refs")


def _validate_handoff_row(
    conn: sqlite3.Connection,
    task_card_id: int,
    task_id: str,
    known_profiles: FrozenSet[str],
    assignee: str,
) -> tuple[bool, list[str]]:
    cur = conn.execute(
        "SELECT version, execution_profile, reviewer, canonical_payload FROM task_handoff_requirements WHERE task_card_id = ? AND task_id = ?",
        (task_card_id, task_id),
    )
    row = cur.fetchone()
    if row is None:
        return False, []
    defects = []
    version, execution_profile, reviewer, canonical_payload = row
    if version != 1:
        defects.append("handoff version must be 1")
    if execution_profile != assignee:
        defects.append("handoff execution_profile mismatch")
    if execution_profile not in known_profiles:
        defects.append("handoff execution_profile not in known profiles")
    try:
        parsed = json.loads(canonical_payload)
        if type(parsed) is not dict:
            defects.append("handoff canonical_payload must be object")
        else:
            normalized = normalize_handoff_requirements(parsed, known_profiles)
            if normalized["reviewer"] != reviewer:
                defects.append("handoff reviewer mismatch")
            if canonical_handoff_requirements(normalized) != canonical_payload:
                defects.append("handoff canonical_payload mismatch")
    except (json.JSONDecodeError, TypeError, ValueError):
        defects.append("handoff canonical_payload malformed")
    return True, defects


def _validate_lifecycle_row(
    conn: sqlite3.Connection,
    task_id: str,
    task_card_id: int,
    initiative_card_id: int,
    initiative_id: str,
    assignee: str,
) -> tuple[dict | None, list[str]]:
    defects = []
    cur = conn.execute(
        """
        SELECT contract_id, contract_version, step, segment_id, workspace_id, execution_profile
        FROM task_lifecycle_contracts WHERE task_card_id = ? AND task_id = ?
        """,
        (task_card_id, task_id),
    )
    raw_row = cur.fetchone()
    if raw_row is None:
        return None, []

    explicit_assoc = {
        "step": raw_row[2],
        "segment_id": raw_row[3],
        "segment_workspace_id": raw_row[4],
        "initiative_id": initiative_id,
        "execution_profile": raw_row[5],
    }

    try:
        record = LifecycleContractRepository(conn).load(task_id)
    except LifecycleRecordRejected:
        defects.append("lifecycle load failed")
        return explicit_assoc, defects

    if record is None:
        return None, []

    if record.task_card_id != task_card_id:
        defects.append("lifecycle task_card_id mismatch")
    if record.task_id != task_id:
        defects.append("lifecycle task_id mismatch")
    if record.initiative_card_id != initiative_card_id:
        defects.append("lifecycle initiative_card_id mismatch")
    if record.snapshot.initiative_id != initiative_id:
        defects.append("lifecycle initiative_id mismatch")
    if record.snapshot.execution_profile != assignee:
        defects.append("lifecycle execution_profile mismatch")

    cur = conn.execute(
        "SELECT declared_inputs_accessible FROM task_input_manifests WHERE task_card_id = ? AND task_id = ?",
        (task_card_id, task_id),
    )
    manifest_row = cur.fetchone()
    if manifest_row is None:
        defects.append("lifecycle input manifest missing")
    elif manifest_row[0] != 1:
        defects.append("lifecycle input manifest declared_inputs_accessible not 1")

    if defects:
        return explicit_assoc, defects

    return {
        "contract_id": record.snapshot.contract_id,
        "contract_version": record.snapshot.contract_version,
        "step": record.snapshot.step,
        "initiative_id": record.snapshot.initiative_id,
        "segment_id": record.snapshot.segment_id,
        "segment_workspace_id": record.snapshot.segment_workspace_id,
        "execution_profile": record.snapshot.execution_profile,
    }, []


def _validate_expected_relations(
    conn: sqlite3.Connection,
    expected: Any,
    predecessor_task_id: str,
    board: str,
    initiative_id: str,
) -> dict:
    if type(expected) is not dict:
        raise ValueError("expected_relations must be an object")
    if set(expected) != {"prerequisite_task_ids", "dependent_task_ids"}:
        raise ValueError(
            "expected_relations must have exactly prerequisite_task_ids and dependent_task_ids"
        )
    prereq_raw = expected["prerequisite_task_ids"]
    dep_raw = expected["dependent_task_ids"]
    if type(prereq_raw) is not list or type(dep_raw) is not list:
        raise ValueError("expected_relations lists must be lists")
    prereq_ids = []
    dep_ids = []
    for item in prereq_raw:
        if not _is_nonblank_str(item):
            raise ValueError("prerequisite_task_ids must contain nonblank strings")
        trimmed = item.strip()
        if trimmed in prereq_ids:
            raise ValueError("duplicate prerequisite_task_id")
        if trimmed == predecessor_task_id:
            raise ValueError("self-link in prerequisites")
        prereq_ids.append(trimmed)
    for item in dep_raw:
        if not _is_nonblank_str(item):
            raise ValueError("dependent_task_ids must contain nonblank strings")
        trimmed = item.strip()
        if trimmed in dep_ids:
            raise ValueError("duplicate dependent_task_id")
        if trimmed == predecessor_task_id:
            raise ValueError("self-link in dependents")
        dep_ids.append(trimmed)
    overlap = set(prereq_ids) & set(dep_ids)
    if overlap:
        raise ValueError("overlap between prerequisites and dependents")
    actual_prereq = set()
    actual_dep = set()
    cur = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ?",
        (predecessor_task_id,),
    )
    for row in cur.fetchall():
        actual_prereq.add(row[0])
    cur = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ?",
        (predecessor_task_id,),
    )
    for row in cur.fetchall():
        actual_dep.add(row[0])
    if tuple(sorted(actual_prereq)) != tuple(sorted(prereq_ids)):
        raise ValueError("prerequisite relations mismatch")
    if tuple(sorted(actual_dep)) != tuple(sorted(dep_ids)):
        raise ValueError("dependent relations mismatch")
    for tid in prereq_ids + dep_ids:
        cur = conn.execute(
            """
            SELECT t.id, t.task_id, t.initiative_id, t.board_slug, n.status
            FROM adrian_kanban_cards t
            JOIN tasks n ON n.id = t.task_id
            WHERE t.card_type = 'task' AND t.task_id = ?
            """,
            (tid,),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            raise ValueError(f"relation endpoint {tid} not found")
        row = rows[0]
        if row[3] != board:
            raise ValueError(f"relation endpoint {tid} board mismatch")
        endpoint_initiative_id = row[2]
        cur_init = conn.execute(
            "SELECT 1 FROM adrian_kanban_cards WHERE initiative_id = ? AND board_slug = ? AND card_type = 'initiative'",
            (endpoint_initiative_id, board),
        )
        if cur_init.fetchone() is None:
            raise ValueError(
                f"initiative card for endpoint {tid} not found on board {board}"
            )
        if tid in dep_ids:
            if row[4] in ("done", "archived", "running", "review"):
                raise ValueError(f"dependent {tid} in terminal or active status")
    return {
        "prerequisite_task_ids": sorted(prereq_ids),
        "dependent_task_ids": sorted(dep_ids),
    }


def _validate_disposition(
    disposition: Any, predecessor_workspace_path: str | None, attachment_paths: tuple
) -> dict:
    if type(disposition) is not dict:
        raise ValueError("repository_worktree_disposition must be an object")
    if set(disposition) != {"reference", "action", "preservation_evidence_ref"}:
        raise ValueError(
            "repository_worktree_disposition must have exactly reference, action, preservation_evidence_ref"
        )
    reference = _normalize_str(disposition["reference"])
    action = disposition["action"]
    preservation_ref = _normalize_str(disposition["preservation_evidence_ref"])
    if action not in ("retain", "cleanup_after_commit"):
        raise ValueError("disposition action must be retain or cleanup_after_commit")
    if action == "cleanup_after_commit":
        if not _is_nonblank_str(predecessor_workspace_path) and not attachment_paths:
            raise ValueError(
                "cleanup_after_commit requires trusted workspace_path or attachment"
            )
    return {
        "reference": reference,
        "action": action,
        "preservation_evidence_ref": preservation_ref,
    }


def _validate_successor_payload(
    payload: Any,
    initiative_id: str,
    board: str,
    predecessor_task_id: str,
    lifecycle_assoc: dict | None,
    handoff_governed: bool,
) -> dict:
    if type(payload) is not dict:
        raise ValueError("successor_payload must be an object")
    if payload.get("initiative_id") != initiative_id:
        raise ValueError("successor_payload initiative_id mismatch")
    if payload.get("board") != board:
        raise ValueError("successor_payload board mismatch")
    task_id = payload.get("task_id")
    if not _is_nonblank_str(task_id):
        raise ValueError("successor_payload task_id must be nonblank")
    task_id = task_id.strip()
    if task_id == predecessor_task_id:
        raise ValueError("successor task_id must differ from predecessor")
    if payload.get("goal_mode") is not True:
        raise ValueError("successor_payload goal_mode must be True")
    if "handoff_requirements_v1" not in payload:
        raise ValueError("successor_payload must contain handoff_requirements_v1")
    if lifecycle_assoc is not None:
        if "lifecycle_contract_v1" not in payload:
            raise ValueError(
                "successor_payload must contain lifecycle_contract_v1 when predecessor has valid lifecycle"
            )
        lc = payload["lifecycle_contract_v1"]
        if type(lc) is not dict:
            raise ValueError("lifecycle_contract_v1 must be an object")
        if lc.get("step") != lifecycle_assoc["step"]:
            raise ValueError("lifecycle_contract_v1 step mismatch")
        if lc.get("segment_id") != lifecycle_assoc["segment_id"]:
            raise ValueError("lifecycle_contract_v1 segment_id mismatch")
        if lc.get("segment_workspace_id") != lifecycle_assoc["segment_workspace_id"]:
            raise ValueError("lifecycle_contract_v1 segment_workspace_id mismatch")
    else:
        if "lifecycle_contract_v1" in payload:
            raise ValueError(
                "successor_payload must not contain lifecycle_contract_v1 when predecessor has no lifecycle"
            )
    return copy.deepcopy(payload)


def admit_purge_replacement(
    conn: sqlite3.Connection,
    *,
    initiative_card_id: int,
    initiative_id: str,
    board: str,
    actor_profile: str,
    session_id: str,
    approval_id: str,
    update: dict,
    known_profiles: Any,
) -> PurgeReplacementAdmission:
    if not conn.in_transaction:
        raise ValueError("must be in an active transaction")
    if not _is_positive_int(initiative_card_id):
        raise ValueError("initiative_card_id must be positive int")
    if not _is_nonblank_str(initiative_id):
        raise ValueError("initiative_id must be nonblank string")
    if not _is_nonblank_str(board):
        raise ValueError("board must be nonblank string")
    if actor_profile != "default":
        raise ValueError("actor_profile must be default")
    if not _is_nonblank_str(session_id):
        raise ValueError("session_id must be nonblank string")
    if not _is_nonblank_str(approval_id):
        raise ValueError("approval_id must be nonblank string")
    if type(update) is not dict:
        raise ValueError("update must be an object")
    required_keys = {
        "replacement_id",
        "predecessor_task_id",
        "predecessor_record_version",
        "eligibility_classification",
        "eligibility_evidence_ref",
        "repository_worktree_disposition",
        "expected_relations",
        "successor_payload",
    }
    if set(update) != required_keys:
        raise ValueError("update must have exactly the required keys")
    profiles = _validate_known_profiles(known_profiles)
    replacement_id = _normalize_str(update["replacement_id"])
    predecessor_task_id = _normalize_str(update["predecessor_task_id"])
    predecessor_record_version = update["predecessor_record_version"]
    if not _is_non_negative_int(predecessor_record_version):
        raise ValueError("predecessor_record_version must be non-negative int")
    eligibility_classification = update["eligibility_classification"]
    if not _is_nonblank_str(eligibility_classification):
        raise ValueError("eligibility_classification must be nonblank string")
    eligibility_evidence_ref = update["eligibility_evidence_ref"]
    disposition_raw = update["repository_worktree_disposition"]
    expected_relations_raw = update["expected_relations"]
    successor_payload_raw = update["successor_payload"]

    initiative_card = _load_initiative_card(
        conn, initiative_card_id, initiative_id, board
    )
    predecessor = _load_predecessor_task(
        conn, predecessor_task_id, initiative_id, board, predecessor_record_version
    )
    _check_active_execution(conn, predecessor_task_id, predecessor)
    _check_accepted_verdict(conn, predecessor["card_id"], predecessor_task_id)
    _check_accepted_phase_refs(conn, initiative_card_id, predecessor_task_id)

    handoff_governed, handoff_defects = _validate_handoff_row(
        conn,
        predecessor["card_id"],
        predecessor_task_id,
        profiles,
        predecessor["assignee"],
    )
    lifecycle_assoc, lifecycle_defects = _validate_lifecycle_row(
        conn,
        predecessor_task_id,
        predecessor["card_id"],
        initiative_card_id,
        initiative_id,
        predecessor["assignee"],
    )
    creation_defects = handoff_defects + lifecycle_defects
    if lifecycle_assoc is not None and not handoff_governed:
        creation_defects.append(
            "handoff requirement missing for lifecycle-associated task"
        )
    if handoff_governed or lifecycle_assoc is not None:
        governed = True
    else:
        governed = False

    if eligibility_classification == "creation_non_compliance":
        if not governed:
            raise ValueError("creation_non_compliance requires governed predecessor")
        if not creation_defects:
            raise ValueError(
                "creation_non_compliance requires at least one creation defect"
            )
        if not _is_nonblank_str(eligibility_evidence_ref):
            raise ValueError(
                "eligibility_evidence_ref must be nonblank string for creation_non_compliance"
            )
    elif eligibility_classification == "permanent_bug_blockage":
        if not governed:
            raise ValueError("permanent_bug_blockage requires governed predecessor")
        if predecessor["status"] != "blocked":
            raise ValueError("permanent_bug_blockage requires blocked status")
        if (
            predecessor["current_run_id"] is not None
            or predecessor["claim_lock"] is not None
            or predecessor["claim_expires"] is not None
            or predecessor["worker_pid"] is not None
        ):
            raise ValueError(
                "permanent_bug_blockage requires no active execution fields"
            )
        if type(eligibility_evidence_ref) is not dict:
            raise ValueError(
                "eligibility_evidence_ref must be object for permanent_bug_blockage"
            )
        if set(eligibility_evidence_ref) != {
            "defect_ref",
            "verification_ref",
            "supported_recovery_attempt_refs",
        }:
            raise ValueError(
                "eligibility_evidence_ref must have exactly defect_ref, verification_ref, supported_recovery_attempt_refs"
            )
        defect_ref = _normalize_str(eligibility_evidence_ref["defect_ref"])
        verification_ref = _normalize_str(eligibility_evidence_ref["verification_ref"])
        recovery = eligibility_evidence_ref["supported_recovery_attempt_refs"]
        if type(recovery) is not dict:
            raise ValueError("supported_recovery_attempt_refs must be object")
        if set(recovery) != {
            "unblock",
            "requeue",
            "retry",
            "input_correction",
            "requested_changes",
        }:
            raise ValueError(
                "supported_recovery_attempt_refs must have exactly unblock, requeue, retry, input_correction, requested_changes"
            )
        for key in (
            "unblock",
            "requeue",
            "retry",
            "input_correction",
            "requested_changes",
        ):
            _normalize_str(recovery[key])
    else:
        raise ValueError(
            "eligibility_classification must be creation_non_compliance or permanent_bug_blockage"
        )

    expected_relations = _validate_expected_relations(
        conn, expected_relations_raw, predecessor_task_id, board, initiative_id
    )

    cur = conn.execute(
        "SELECT stored_path FROM task_attachments WHERE task_id = ?",
        (predecessor_task_id,),
    )
    attachment_paths = tuple(row[0] for row in cur.fetchall())

    disposition = _validate_disposition(
        disposition_raw, predecessor["workspace_path"], attachment_paths
    )

    if (
        lifecycle_assoc is not None
        and lifecycle_assoc.get("segment_workspace_id") is not None
    ):
        if disposition["action"] != "retain":
            raise ValueError(
                "lifecycle association with segment_workspace_id requires retain disposition"
            )

    successor_task_id = successor_payload_raw.get("task_id")
    if _is_nonblank_str(successor_task_id):
        successor_task_id = successor_task_id.strip()
        cur = conn.execute(
            "SELECT 1 FROM task_purge_replacements WHERE predecessor_task_id = ?",
            (successor_task_id,),
        )
        if cur.fetchone() is not None:
            raise ValueError(
                "successor task_id has already appeared as a predecessor in a purge replacement"
            )

    successor_payload = _validate_successor_payload(
        successor_payload_raw,
        initiative_id,
        board,
        predecessor_task_id,
        lifecycle_assoc,
        handoff_governed,
    )

    if "parents" in successor_payload:
        parents = successor_payload["parents"]
        if type(parents) is not list:
            raise ValueError("successor_payload parents must be a list")
        normalized_parents = []
        for p in parents:
            if not _is_nonblank_str(p):
                raise ValueError(
                    "successor_payload parents must contain nonblank strings"
                )
            normalized_parents.append(p.strip())
        if len(normalized_parents) != len(set(normalized_parents)):
            raise ValueError("duplicate parents in successor_payload")
        if tuple(sorted(normalized_parents)) != tuple(
            sorted(expected_relations["prerequisite_task_ids"])
        ):
            raise ValueError(
                "successor_payload parents mismatch expected prerequisites"
            )

    return PurgeReplacementAdmission(
        replacement_id=replacement_id,
        initiative_card_id=initiative_card_id,
        initiative_id=initiative_id,
        board=board,
        actor_profile=actor_profile,
        session_id=session_id,
        approval_id=approval_id,
        predecessor_task_card_id=predecessor["card_id"],
        predecessor_task_id=predecessor_task_id,
        predecessor_record_version=predecessor_record_version,
        eligibility_classification=eligibility_classification,
        eligibility_evidence_ref=eligibility_evidence_ref,
        repository_worktree_disposition=disposition,
        expected_relations=expected_relations,
        successor_payload=successor_payload,
        handoff_governed=handoff_governed,
        creation_defects=tuple(creation_defects),
        lifecycle_association=lifecycle_assoc,
        predecessor_workspace_kind=predecessor["workspace_kind"],
        predecessor_workspace_path=predecessor["workspace_path"],
        predecessor_branch_name=predecessor["branch_name"],
        predecessor_project_id=predecessor["project_id"],
        predecessor_attachment_paths=attachment_paths,
    )


def finalize_purge_replacement(
    conn: sqlite3.Connection,
    admission: PurgeReplacementAdmission,
    *,
    successor_task_card_id: int,
    created_at: int,
) -> None:
    if type(admission) is not PurgeReplacementAdmission:
        raise ValueError("admission must be PurgeReplacementAdmission")
    if not conn.in_transaction:
        raise ValueError("must be in an active transaction")
    if not _is_positive_int(successor_task_card_id):
        raise ValueError("successor_task_card_id must be positive int")
    if not _is_positive_int(created_at):
        raise ValueError("created_at must be positive int")

    successor_task_id = admission.successor_payload["task_id"]
    cur = conn.execute(
        """
        SELECT t.id, t.task_id, t.initiative_id, t.board_slug, t.record_version,
               n.status, n.current_run_id, n.claim_lock, n.claim_expires, n.worker_pid,
               n.consecutive_failures
        FROM adrian_kanban_cards t
        JOIN tasks n ON n.id = t.task_id
        WHERE t.id = ? AND t.card_type = 'task'
        """,
        (successor_task_card_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError("successor card not found")
    if row[1] != successor_task_id:
        raise ValueError("successor task_id mismatch")
    if row[2] != admission.initiative_id:
        raise ValueError("successor initiative_id mismatch")
    if row[3] != admission.board:
        raise ValueError("successor board mismatch")
    if row[4] != 0:
        raise ValueError("successor record_version must be 0")
    if row[5] not in ("todo", "ready"):
        raise ValueError("successor status must be todo or ready")
    if (
        row[6] is not None
        or row[7] is not None
        or row[8] is not None
        or row[9] is not None
    ):
        raise ValueError("successor must have no active execution fields")
    if row[10] != 0:
        raise ValueError("successor consecutive_failures must be 0")

    cur = conn.execute(
        "SELECT 1 FROM task_handoff_requirements WHERE task_card_id = ? AND task_id = ?",
        (successor_task_card_id, successor_task_id),
    )
    if cur.fetchone() is None:
        raise ValueError("successor handoff requirement missing")

    if admission.lifecycle_association is not None:
        try:
            record = LifecycleContractRepository(conn).load(successor_task_id)
        except LifecycleRecordRejected:
            raise ValueError("successor lifecycle load failed")
        if record is None:
            raise ValueError("successor lifecycle missing")
        assoc = admission.lifecycle_association
        if "contract_id" in assoc:
            if record.snapshot.contract_id != assoc["contract_id"]:
                raise ValueError("successor lifecycle contract_id mismatch")
            if record.snapshot.contract_version != assoc["contract_version"]:
                raise ValueError("successor lifecycle contract_version mismatch")
        if record.snapshot.step != assoc["step"]:
            raise ValueError("successor lifecycle step mismatch")
        if record.snapshot.initiative_id != assoc["initiative_id"]:
            raise ValueError("successor lifecycle initiative_id mismatch")
        if record.snapshot.segment_id != assoc["segment_id"]:
            raise ValueError("successor lifecycle segment_id mismatch")
        if record.snapshot.segment_workspace_id != assoc["segment_workspace_id"]:
            raise ValueError("successor lifecycle segment_workspace_id mismatch")
        if record.snapshot.execution_profile != assoc["execution_profile"]:
            raise ValueError("successor lifecycle execution_profile mismatch")
    else:
        cur = conn.execute(
            "SELECT 1 FROM task_lifecycle_contracts WHERE task_card_id = ?",
            (successor_task_card_id,),
        )
        if cur.fetchone() is not None:
            raise ValueError(
                "successor must not have lifecycle when predecessor has none"
            )

    actual_prereq = set()
    cur = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ?",
        (successor_task_id,),
    )
    for r in cur.fetchall():
        actual_prereq.add(r[0])
    if tuple(sorted(actual_prereq)) != tuple(
        sorted(admission.expected_relations["prerequisite_task_ids"])
    ):
        raise ValueError("successor prerequisite links mismatch")

    for dep_id in admission.expected_relations["dependent_task_ids"]:
        cur = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (successor_task_id, dep_id),
        )
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (successor_task_id, dep_id),
            )
            cur = conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
                (successor_task_id, dep_id),
            )
            if cur.fetchone() is None:
                raise ValueError(f"failed to create link for dependent {dep_id}")
        cur = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (dep_id,),
        )
        dep_status = cur.fetchone()
        if dep_status is None:
            raise ValueError(f"dependent {dep_id} not found")
        if dep_status[0] in ("done", "archived"):
            raise ValueError(f"dependent {dep_id} in terminal status")

    cur = conn.execute(
        """
        SELECT task_id, platform, chat_id, thread_id, user_id, user_id_alt,
               chat_type, notifier_profile, delivery_mode, delivery_metadata,
               created_at, last_event_id
        FROM kanban_notify_subs WHERE task_id = ?
        """,
        (admission.predecessor_task_id,),
    )
    for sub in cur.fetchall():
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
             chat_type, notifier_profile, delivery_mode, delivery_metadata,
             created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                successor_task_id,
                sub[1],
                sub[2],
                sub[3],
                sub[4],
                sub[5],
                sub[6],
                sub[7],
                sub[8],
                sub[9],
                sub[10],
            ),
        )
    cur = conn.execute(
        "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
        (successor_task_id,),
    )
    sub_count = cur.fetchone()[0]
    cur = conn.execute(
        "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
        (admission.predecessor_task_id,),
    )
    pred_sub_count = cur.fetchone()[0]
    if sub_count != pred_sub_count:
        raise ValueError("notification copy count mismatch")

    workspace_snapshot = {
        "workspace_kind": admission.predecessor_workspace_kind,
        "workspace_path": admission.predecessor_workspace_path,
        "branch_name": admission.predecessor_branch_name,
        "project_id": admission.predecessor_project_id,
        "attachment_paths": list(admission.predecessor_attachment_paths),
    }
    cleanup_required = (
        1
        if admission.repository_worktree_disposition["action"] == "cleanup_after_commit"
        else 0
    )
    conn.execute(
        """
        INSERT INTO task_purge_replacements (
            replacement_id, initiative_card_id, initiative_id, board_slug,
            predecessor_task_card_id, predecessor_task_id,
            successor_task_card_id, successor_task_id,
            eligibility_classification, eligibility_evidence,
            authorization_approval_id, requester_evidence,
            repository_disposition_reference, repository_disposition_action,
            repository_disposition_preservation_ref, transferred_relations,
            cleanup_required, predecessor_workspace_snapshot, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            admission.replacement_id,
            admission.initiative_card_id,
            admission.initiative_id,
            admission.board,
            admission.predecessor_task_card_id,
            admission.predecessor_task_id,
            successor_task_card_id,
            successor_task_id,
            admission.eligibility_classification,
            _canonical_json(admission.eligibility_evidence_ref),
            admission.approval_id,
            _canonical_json({
                "actor_profile": admission.actor_profile,
                "session_id": admission.session_id,
            }),
            admission.repository_worktree_disposition["reference"],
            admission.repository_worktree_disposition["action"],
            admission.repository_worktree_disposition["preservation_evidence_ref"],
            _canonical_json(admission.expected_relations),
            cleanup_required,
            _canonical_json(workspace_snapshot),
            created_at,
        ),
    )

    conn.execute(
        "DELETE FROM task_reviewer_verdicts WHERE task_card_id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    conn.execute(
        "DELETE FROM task_handoff_rejections WHERE task_card_id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    conn.execute(
        "DELETE FROM task_candidate_handoffs WHERE task_card_id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    conn.execute(
        "DELETE FROM task_lifecycle_contracts WHERE task_card_id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    conn.execute(
        "DELETE FROM task_input_entries WHERE task_card_id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    conn.execute(
        "DELETE FROM task_input_manifests WHERE task_card_id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    conn.execute(
        "DELETE FROM task_handoff_requirements WHERE task_card_id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    conn.execute(
        "DELETE FROM kanban_notify_subs WHERE task_id = ?",
        (admission.predecessor_task_id,),
    )
    conn.execute(
        "DELETE FROM task_comments WHERE task_id = ?",
        (admission.predecessor_task_id,),
    )
    conn.execute(
        "DELETE FROM task_events WHERE task_id = ?",
        (admission.predecessor_task_id,),
    )
    conn.execute(
        "DELETE FROM task_attachments WHERE task_id = ?",
        (admission.predecessor_task_id,),
    )
    conn.execute(
        "DELETE FROM task_runs WHERE task_id = ?",
        (admission.predecessor_task_id,),
    )
    conn.execute(
        "DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
        (admission.predecessor_task_id, admission.predecessor_task_id),
    )
    cur = conn.execute(
        "DELETE FROM adrian_kanban_cards WHERE id = ? AND task_id = ?",
        (admission.predecessor_task_card_id, admission.predecessor_task_id),
    )
    if cur.rowcount != 1:
        raise ValueError("predecessor card deletion failed")
    cur = conn.execute(
        "DELETE FROM tasks WHERE id = ?",
        (admission.predecessor_task_id,),
    )
    if cur.rowcount != 1:
        raise ValueError("predecessor task deletion failed")
    from .purge_cleanup import prepare_cleanup

    prepare_cleanup(conn, admission.replacement_id)

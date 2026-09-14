"""Server-derived repository evidence for Initiative Tracker transitions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .coordination_materialization import CoordinationMaterializer
from .coordination_workspace import CoordinationWorkspaceStore
from .published_git import _git as _git_bytes
from .repository_binding import RepositoryBindingError, RepositoryBindingResolver
from .workspace import (
    _GitWorkspaceExecutor,
    _TrustedRepositoryRegistry,
    _WorkspaceMember,
    _WorkspaceRejected,
    _load_active_segment_workspace,
    _load_segment_workspace,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_PHASES = frozenset({"DEV2", "DEV3", "DEV4"})
_INITIATIVE_PHASES = frozenset({
    "D1",
    "D2",
    "D3",
    "D4",
    "DEV1",
    "DEV2",
    "DEV3",
    "DEV4",
    "PC1",
})
_BASE_RESULT_FIELDS = frozenset({
    "initiative_id",
    "board",
    "previous_transition_id",
    "from_phase",
    "from_segment_id",
    "to_phase",
    "to_segment_id",
    "canon_route",
    "exit_gate_ref",
    "verification_result",
})


class RepositoryReconciliationError(ValueError):
    """Live repository evidence does not satisfy the requested boundary."""


@dataclass(frozen=True)
class PreparedRepositoryReconciliation:
    """Canonical, server-derived reconciliation result ready for admission."""

    canonical_json: str

    def result(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise RepositoryReconciliationError(
                "prepared reconciliation must decode to an object"
            )
        return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _integration_required(from_phase: str, to_phase: str) -> bool:
    return (
        to_phase == "PC1"
        or (from_phase, to_phase)
        in {
            ("D1", "D2"),
            ("D2", "D3"),
            ("D3", "D4"),
            ("D4", "DEV1"),
        }
        or from_phase == "DEV4"
    )


def _exact_integration_required(to_phase: str) -> bool:
    return to_phase == "PC1"


def _members(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    *,
    initiative_id: str,
    from_phase: str,
    from_segment_id: str | None,
) -> tuple[str, tuple[_WorkspaceMember, ...], bool]:
    if from_phase in _SEGMENT_PHASES:
        if not isinstance(from_segment_id, str) or not from_segment_id.strip():
            raise RepositoryReconciliationError(
                f"{from_phase} reconciliation requires a current segment"
            )
        if from_phase == "DEV4":
            workspace_id, _, plan = _load_segment_workspace(
                conn,
                registry,
                initiative_id=initiative_id,
                segment_id=from_segment_id,
            )
            row = conn.execute(
                "SELECT lifecycle_state, active FROM segment_workspaces "
                "WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None or row[0] != "retired" or row[1] != 0:
                raise RepositoryReconciliationError(
                    "DEV4 reconciliation requires a retired segment workspace"
                )
            if any(member.member_state != "retired" for member in plan.members):
                raise RepositoryReconciliationError(
                    "DEV4 reconciliation requires every segment member to be retired"
                )
            return workspace_id, plan.members, True
        workspace_id, _, plan = _load_active_segment_workspace(
            conn,
            registry,
            initiative_id=initiative_id,
            segment_id=from_segment_id,
        )
        return workspace_id, plan.members, False

    if from_segment_id is not None:
        raise RepositoryReconciliationError(
            f"non-segment phase {from_phase} must not have a current segment"
        )
    workspace = CoordinationWorkspaceStore(conn).read_active(initiative_id)
    if workspace.get("lifecycle_state") != "materialized":
        raise RepositoryReconciliationError(
            "coordination workspace must be materialized before reconciliation"
        )
    materializer = CoordinationMaterializer(conn, registry)
    members = tuple(
        materializer._build_workspace_member(
            workspace["workspace_id"], initiative_id, member
        )
        for member in workspace.get("members", ())
    )
    if not members:
        raise RepositoryReconciliationError(
            "coordination workspace has no repository members"
        )
    return workspace["workspace_id"], members, False


def _validate_reference_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise RepositoryReconciliationError(
            "exit_gate_ref.path must be a nonblank repository-relative path"
        )
    normalized = path.strip().replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise RepositoryReconciliationError(
            "exit_gate_ref.path must remain within its registered repository"
        )
    return candidate.as_posix()


def _artifact_evidence(
    registry: _TrustedRepositoryRegistry,
    executor: _GitWorkspaceExecutor,
    reference: Any,
    *,
    allowed_repository_ids: set[str],
    exact_integration: bool,
) -> dict[str, str]:
    if not isinstance(reference, dict) or set(reference) != {
        "repository_identity",
        "path",
        "commit",
        "sha256",
    }:
        raise RepositoryReconciliationError(
            "exit_gate_ref must contain exactly repository_identity, path, commit, "
            "and sha256"
        )
    repository_identity = reference.get("repository_identity")
    if not isinstance(repository_identity, str) or not repository_identity.strip():
        raise RepositoryReconciliationError(
            "exit_gate_ref.repository_identity must be nonblank"
        )
    repository_identity = repository_identity.strip()
    if repository_identity not in allowed_repository_ids:
        raise RepositoryReconciliationError(
            "exit_gate_ref repository is not an affected initiative member"
        )
    path = _validate_reference_path(reference.get("path"))
    commit = reference.get("commit")
    sha256 = reference.get("sha256")
    if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
        raise RepositoryReconciliationError(
            "exit_gate_ref.commit must be a full lowercase 40-character SHA"
        )
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise RepositoryReconciliationError(
            "exit_gate_ref.sha256 must be a lowercase 64-character SHA-256"
        )

    registration = registry.lookup(repository_identity)
    try:
        resolved = RepositoryBindingResolver(registration).resolve_integration_head(
            allow_offline=False
        )
    except RepositoryBindingError as exc:
        raise RepositoryReconciliationError(
            f"exit-gate repository integration head could not be verified: {exc}"
        ) from exc
    if exact_integration:
        if commit != resolved.head_sha:
            raise RepositoryReconciliationError(
                "entry to PC1 requires the exit-gate artifact commit to equal the "
                "current integration head"
            )
    elif not executor._is_ancestor(
        registration.repository_root, commit, resolved.head_sha
    ):
        raise RepositoryReconciliationError(
            "exit-gate artifact commit is not contained by the integration branch"
        )
    blob_result = _git_bytes(
        registration.repository_root,
        "cat-file",
        "blob",
        f"{commit}:{path}",
    )
    if blob_result.returncode != 0:
        raise RepositoryReconciliationError(
            "exit-gate artifact cannot be read at the pinned commit and path"
        )
    blob = blob_result.stdout
    if hashlib.sha256(blob).hexdigest() != sha256:
        raise RepositoryReconciliationError(
            "exit-gate artifact SHA-256 does not match the published blob"
        )
    return {
        "repository_identity": repository_identity,
        "path": path,
        "commit": commit,
        "sha256": sha256,
        "integration_ref": resolved.remote_ref,
        "integration_head": resolved.head_sha,
    }


def _member_evidence(
    registry: _TrustedRepositoryRegistry,
    executor: _GitWorkspaceExecutor,
    member: _WorkspaceMember,
    *,
    integration_required: bool,
    exact_integration: bool,
    sealed: bool,
) -> dict[str, Any]:
    registration = registry.lookup(member.repository_identity)
    try:
        integration = RepositoryBindingResolver(registration).resolve_integration_head(
            allow_offline=False
        )
    except RepositoryBindingError as exc:
        raise RepositoryReconciliationError(
            f"member {member.repository_identity}: integration head verification "
            f"failed: {exc}"
        ) from exc

    if sealed:
        local_head = member.observed_head
        if not isinstance(local_head, str) or not _SHA_RE.fullmatch(local_head):
            raise RepositoryReconciliationError(
                f"member {member.repository_identity}: retired head is invalid"
            )
        clean = True
    else:
        if member.member_state != "materialized":
            raise RepositoryReconciliationError(
                f"member {member.repository_identity}: expected materialized state, "
                f"observed {member.member_state!r}"
            )
        verification = executor.verify(member)
        if not verification.ready or verification.observed_head is None:
            raise RepositoryReconciliationError(
                f"member {member.repository_identity}: workspace verification failed: "
                f"{verification.failures}"
            )
        local_head = verification.observed_head
        status = executor._git(
            member.target_path,
            "status",
            "--porcelain",
            "--untracked-files=all",
        )
        clean = not status.strip()
        if not clean:
            raise RepositoryReconciliationError(
                f"member {member.repository_identity}: worktree has unexplained "
                "staged, modified, deleted, or untracked paths"
            )

    if integration_required:
        contained = executor._is_ancestor(
            registration.repository_root,
            local_head,
            integration.head_sha,
        )
        if not contained:
            raise RepositoryReconciliationError(
                f"member {member.repository_identity}: HEAD {local_head} is not "
                f"contained by {integration.remote_ref} at {integration.head_sha}"
            )
        if exact_integration and local_head != integration.head_sha:
            raise RepositoryReconciliationError(
                f"member {member.repository_identity}: entry to PC1 requires exact "
                f"integration baseline {integration.head_sha}, observed {local_head}"
            )
        delivery_ref = integration.remote_ref
        delivery_head = integration.head_sha
        condition = (
            "exact_integration_head" if exact_integration else "integration_contained"
        )
    else:
        fetch_ref = f"refs/heads/{member.branch}:refs/remotes/origin/{member.branch}"
        executor._git(
            registration.repository_root,
            "fetch",
            "--no-tags",
            "origin",
            fetch_ref,
        )
        delivery_ref = f"refs/remotes/origin/{member.branch}"
        delivery_head = executor._git(
            registration.repository_root,
            "rev-parse",
            delivery_ref,
        )
        if local_head != delivery_head:
            raise RepositoryReconciliationError(
                f"member {member.repository_identity}: local HEAD {local_head} is not "
                f"the published assigned-branch head {delivery_head}"
            )
        contained = local_head == delivery_head
        condition = "assigned_branch_head"

    return {
        "repository_identity": member.repository_identity,
        "github_repository": registration.github_repository,
        "worktree_path": member.target_path,
        "branch": member.branch,
        "required_base_sha": member.required_base_sha,
        "head": local_head,
        "member_state": member.member_state,
        "clean_including_untracked": clean,
        "dirty_path_dispositions": [],
        "delivery_condition": condition,
        "delivery_ref": delivery_ref,
        "delivery_head": delivery_head,
        "integration_ref": integration.remote_ref,
        "integration_head": integration.head_sha,
        "remote_containment": contained,
    }


def prepare_repository_reconciliation(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    result: Any,
) -> PreparedRepositoryReconciliation:
    """Derive a complete proof and reject caller-supplied or stale evidence."""
    if not isinstance(conn, sqlite3.Connection):
        raise RepositoryReconciliationError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        raise RepositoryReconciliationError(
            "repository reconciliation requires no active database transaction"
        )
    if not isinstance(registry, _TrustedRepositoryRegistry):
        raise RepositoryReconciliationError(
            "trusted repository registry is required for reconciliation"
        )
    if not isinstance(result, dict):
        raise RepositoryReconciliationError("reconciliation result must be an object")
    fields = set(result)
    if fields not in {_BASE_RESULT_FIELDS, _BASE_RESULT_FIELDS | {"repository_proof"}}:
        raise RepositoryReconciliationError(
            "reconciliation result has an invalid field set"
        )
    base = {key: result[key] for key in _BASE_RESULT_FIELDS}
    for name in ("initiative_id", "board", "from_phase", "to_phase", "canon_route"):
        value = base.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RepositoryReconciliationError(f"{name} must be a nonblank string")
    if base.get("verification_result") != "accepted":
        raise RepositoryReconciliationError(
            "verification_result must request the accepted reconciliation outcome"
        )
    rows = conn.execute(
        "SELECT id FROM adrian_kanban_cards WHERE initiative_id = ? "
        "AND board_slug = ? AND card_type = 'initiative' AND task_id IS NULL "
        "AND closed_at IS NULL",
        (base["initiative_id"], base["board"]),
    ).fetchall()
    if len(rows) != 1:
        raise RepositoryReconciliationError(
            "reconciliation must target exactly one open initiative in its board"
        )
    latest = conn.execute(
        "SELECT transition_id, to_phase, to_segment_id "
        "FROM initiative_transitions WHERE initiative_id = ? "
        "ORDER BY transition_id DESC LIMIT 1",
        (base["initiative_id"],),
    ).fetchone()
    if latest is None:
        raise RepositoryReconciliationError(
            "reconciliation initiative has no lifecycle position"
        )
    if (
        base.get("previous_transition_id") != latest[0]
        or base["from_phase"] != latest[1]
        or base.get("from_segment_id") != latest[2]
    ):
        raise RepositoryReconciliationError(
            "reconciliation source does not match the current lifecycle position"
        )
    if base["to_phase"] not in _INITIATIVE_PHASES:
        raise RepositoryReconciliationError(
            "reconciliation destination is not a recognized lifecycle phase"
        )
    target_segment = base.get("to_segment_id")
    if base["to_phase"] in _SEGMENT_PHASES:
        if not isinstance(target_segment, str) or not target_segment.strip():
            raise RepositoryReconciliationError(
                "reconciliation destination requires a nonblank segment"
            )
    elif target_segment is not None:
        raise RepositoryReconciliationError(
            "reconciliation destination segment must be absent"
        )
    workspace_id, members, sealed = _members(
        conn,
        registry,
        initiative_id=base["initiative_id"],
        from_phase=base["from_phase"],
        from_segment_id=base.get("from_segment_id"),
    )
    integration_required = _integration_required(base["from_phase"], base["to_phase"])
    exact_integration = _exact_integration_required(base["to_phase"])
    executor = _GitWorkspaceExecutor()
    member_proofs = [
        _member_evidence(
            registry,
            executor,
            member,
            integration_required=integration_required,
            exact_integration=exact_integration,
            sealed=sealed,
        )
        for member in sorted(members, key=lambda item: item.repository_identity)
    ]
    artifact = _artifact_evidence(
        registry,
        executor,
        base["exit_gate_ref"],
        allowed_repository_ids={member.repository_identity for member in members},
        exact_integration=exact_integration,
    )
    proof = {
        "proof_version": 1,
        "workspace_id": workspace_id,
        "required_condition": (
            "exact_integration_head"
            if exact_integration
            else "integration_contained"
            if integration_required
            else "assigned_branch_head"
        ),
        "exit_gate_artifact": artifact,
        "members": member_proofs,
    }
    supplied = result.get("repository_proof")
    if supplied is not None and supplied != proof:
        raise RepositoryReconciliationError(
            "approved repository proof is stale or does not match live repository state"
        )
    canonical_result = dict(base)
    canonical_result["repository_proof"] = proof
    return PreparedRepositoryReconciliation(_canonical(canonical_result))


def revalidate_repository_reconciliation(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    canonical_result: Any,
) -> dict[str, Any]:
    prepared = prepare_repository_reconciliation(conn, registry, canonical_result)
    return prepared.result()


def revalidate_stored_repository_reconciliation(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    reconciliation_ref: str,
) -> dict[str, Any]:
    if not isinstance(reconciliation_ref, str) or not reconciliation_ref.strip():
        raise RepositoryReconciliationError(
            "reconciliation_ref must be a nonblank string"
        )
    row = conn.execute(
        "SELECT result_kind, accepted, canonical_payload "
        "FROM initiative_phase_results WHERE result_id = ?",
        (reconciliation_ref.strip(),),
    ).fetchone()
    if row is None:
        raise RepositoryReconciliationError("repository reconciliation was not found")
    if row[0] != "repository_reconciliation" or row[1] != 1:
        raise RepositoryReconciliationError(
            "repository reconciliation is not an accepted reconciliation record"
        )
    try:
        canonical_result = json.loads(row[2])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RepositoryReconciliationError(
            "stored repository reconciliation is malformed"
        ) from exc
    return revalidate_repository_reconciliation(conn, registry, canonical_result)


__all__ = [
    "PreparedRepositoryReconciliation",
    "RepositoryReconciliationError",
    "prepare_repository_reconciliation",
    "revalidate_repository_reconciliation",
    "revalidate_stored_repository_reconciliation",
]

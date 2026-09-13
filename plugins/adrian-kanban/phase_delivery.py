"""Immutable repository delivery proof for initiative phase transitions."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

from .coordination_materialization import CoordinationMaterializer
from .coordination_workspace import CoordinationWorkspaceStore
from .workspace import (
    _GitWorkspaceExecutor,
    _TrustedRepositoryRegistry,
    _WorkspaceMember,
    _WorkspaceRejected,
    _load_active_segment_workspace,
    _load_segment_workspace,
)

__all__ = [
    "PhaseDeliveryError",
    "verify_transition_delivery",
]


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SEGMENT_PHASES = frozenset({"DEV2", "DEV3", "DEV4"})


class PhaseDeliveryError(ValueError):
    """A phase boundary lacks complete, immutable repository evidence."""


def _artifact_commits(value: Any) -> set[str]:
    commits: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "commit" and isinstance(child, str) and _SHA_RE.fullmatch(child):
                commits.add(child)
            else:
                commits.update(_artifact_commits(child))
    elif isinstance(value, list):
        for child in value:
            commits.update(_artifact_commits(child))
    return commits


def _load_phase_commits(conn: sqlite3.Connection, phase_close_ref: str) -> tuple[str, ...]:
    row = conn.execute(
        "SELECT canonical_payload FROM initiative_phase_results WHERE result_id = ?",
        (phase_close_ref,),
    ).fetchone()
    if row is None:
        return ()
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PhaseDeliveryError(
            "phase-close canonical payload is not valid JSON"
        ) from exc
    return tuple(sorted(_artifact_commits(payload)))


def _coordination_members(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    initiative_id: str,
) -> tuple[str, tuple[_WorkspaceMember, ...]]:
    workspace = CoordinationWorkspaceStore(conn).read_active(initiative_id)
    if workspace.get("lifecycle_state") != "materialized":
        raise PhaseDeliveryError(
            "coordination workspace must be materialized before phase transition"
        )
    materializer = CoordinationMaterializer(conn, registry)
    members = tuple(
        materializer._build_workspace_member(
            workspace["workspace_id"], initiative_id, member
        )
        for member in workspace.get("members", ())
    )
    if not members:
        raise PhaseDeliveryError("coordination workspace has no repository members")
    return workspace["workspace_id"], members


def _segment_members(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    initiative_id: str,
    segment_id: str,
    from_phase: str,
) -> tuple[str, tuple[_WorkspaceMember, ...], bool]:
    if from_phase == "DEV4":
        workspace_id, _, plan = _load_segment_workspace(
            conn,
            registry,
            initiative_id=initiative_id,
            segment_id=segment_id,
        )
        row = conn.execute(
            "SELECT lifecycle_state, active FROM segment_workspaces "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None or row[0] != "retired" or row[1] != 0:
            raise PhaseDeliveryError(
                "DEV4 transition requires the segment workspace to be retired"
            )
        if any(member.member_state != "retired" for member in plan.members):
            raise PhaseDeliveryError(
                "DEV4 transition requires every segment member to be retired"
            )
        return workspace_id, plan.members, True

    workspace_id, _, plan = _load_active_segment_workspace(
        conn,
        registry,
        initiative_id=initiative_id,
        segment_id=segment_id,
    )
    return workspace_id, plan.members, False


def _fetch(executor: _GitWorkspaceExecutor, member: _WorkspaceMember) -> None:
    executor._git(member.repository_root, "fetch", "--no-tags", "origin")


def _verify_remote_branch_head(
    executor: _GitWorkspaceExecutor,
    member: _WorkspaceMember,
    local_head: str,
) -> str:
    _fetch(executor, member)
    remote_ref = f"refs/remotes/origin/{member.branch}"
    try:
        remote_head = executor._git(member.repository_root, "rev-parse", remote_ref)
    except _WorkspaceRejected as exc:
        raise PhaseDeliveryError(
            f"member {member.repository_identity}: remote branch {member.branch!r} "
            "is missing; push the phase-closing commit and retry"
        ) from exc
    if remote_head != local_head:
        raise PhaseDeliveryError(
            f"member {member.repository_identity}: local HEAD {local_head} is not "
            f"the published remote branch head {remote_head}; synchronize and push "
            "the complete phase commit, then retry"
        )
    return remote_head


def _verify_remote_main_contains(
    executor: _GitWorkspaceExecutor,
    member: _WorkspaceMember,
    delivered_head: str,
) -> str:
    _fetch(executor, member)
    remote_main = executor._git(
        member.repository_root, "rev-parse", "refs/remotes/origin/main"
    )
    if not executor._is_ancestor(member.repository_root, delivered_head, remote_main):
        raise PhaseDeliveryError(
            f"member {member.repository_identity}: retired segment head "
            f"{delivered_head} is not contained by remote main {remote_main}"
        )
    return remote_main


def _commit_is_delivered(
    executor: _GitWorkspaceExecutor,
    members: Iterable[_WorkspaceMember],
    member_heads: dict[str, str],
    commit: str,
) -> bool:
    for member in members:
        try:
            if executor._is_ancestor(
                member.repository_root,
                commit,
                member_heads[member.repository_identity],
            ):
                return True
        except _WorkspaceRejected:
            continue
    return False


def verify_transition_delivery(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    *,
    initiative_id: str,
    from_phase: str,
    from_segment_id: str | None,
    phase_close_ref: str,
    executor: _GitWorkspaceExecutor | None = None,
) -> dict[str, Any]:
    """Prove that accepted phase work is clean, committed and published.

    The accepted phase result remains the completeness oracle.  This verifier
    adds the repository proof that the resulting files are on the assigned
    branch at an immutable, remotely published commit.  DEV4 consumes the
    stronger sealed proof that the retired segment head is contained by remote
    main.
    """
    if type(conn) is not sqlite3.Connection:
        raise PhaseDeliveryError("conn must be a sqlite3.Connection")
    if not isinstance(registry, _TrustedRepositoryRegistry):
        raise PhaseDeliveryError("registry must be a trusted repository registry")
    for name, value in (
        ("initiative_id", initiative_id),
        ("from_phase", from_phase),
        ("phase_close_ref", phase_close_ref),
    ):
        if not isinstance(value, str) or not value.strip():
            raise PhaseDeliveryError(f"{name} must be a nonblank string")
    if from_phase in _SEGMENT_PHASES:
        if not isinstance(from_segment_id, str) or not from_segment_id.strip():
            raise PhaseDeliveryError(
                f"{from_phase} delivery proof requires a current segment"
            )
    elif from_segment_id is not None:
        raise PhaseDeliveryError(
            f"non-segment phase {from_phase} must not have a current segment"
        )
    if conn.in_transaction:
        raise PhaseDeliveryError("delivery proof requires no active transaction")

    git = executor or _GitWorkspaceExecutor()
    if from_phase in _SEGMENT_PHASES:
        workspace_id, members, sealed = _segment_members(
            conn,
            registry,
            initiative_id,
            from_segment_id,
            from_phase,
        )
    else:
        workspace_id, members = _coordination_members(conn, registry, initiative_id)
        sealed = False

    member_evidence: list[dict[str, str]] = []
    delivered_heads: dict[str, str] = {}
    for member in sorted(members, key=lambda item: item.repository_identity):
        if sealed:
            delivered_head = member.observed_head
            if not isinstance(delivered_head, str) or not _SHA_RE.fullmatch(delivered_head):
                raise PhaseDeliveryError(
                    f"member {member.repository_identity}: retired delivery head is invalid"
                )
            remote_head = _verify_remote_main_contains(git, member, delivered_head)
            remote_ref = "refs/remotes/origin/main"
        else:
            if member.member_state != "materialized":
                raise PhaseDeliveryError(
                    f"member {member.repository_identity}: expected materialized state, "
                    f"observed {member.member_state!r}"
                )
            verification = git.verify(member)
            if not verification.ready or verification.observed_head is None:
                raise PhaseDeliveryError(
                    f"member {member.repository_identity}: workspace verification "
                    f"failed: {verification.failures}"
                )
            status = git._git(
                member.target_path,
                "status",
                "--porcelain",
                "--untracked-files=all",
            )
            if status.strip():
                raise PhaseDeliveryError(
                    f"member {member.repository_identity}: worktree is dirty; review "
                    "the complete diff, commit every intended phase change, and retry"
                )
            delivered_head = verification.observed_head
            remote_head = _verify_remote_branch_head(git, member, delivered_head)
            remote_ref = f"refs/remotes/origin/{member.branch}"

        delivered_heads[member.repository_identity] = delivered_head
        member_evidence.append(
            {
                "repository_identity": member.repository_identity,
                "branch": member.branch,
                "head": delivered_head,
                "remote_ref": remote_ref,
                "remote_head": remote_head,
                "state": member.member_state,
            }
        )

    artifact_commits = _load_phase_commits(conn, phase_close_ref)
    for commit in artifact_commits:
        if not _commit_is_delivered(git, members, delivered_heads, commit):
            raise PhaseDeliveryError(
                f"phase artifact commit {commit} is not contained by any delivered "
                "repository member head"
            )

    return {
        "proof_version": 1,
        "workspace_id": workspace_id,
        "phase": from_phase,
        "segment_id": from_segment_id,
        "phase_close_ref": phase_close_ref,
        "clean_including_untracked": True,
        "artifact_commits": list(artifact_commits),
        "members": member_evidence,
    }

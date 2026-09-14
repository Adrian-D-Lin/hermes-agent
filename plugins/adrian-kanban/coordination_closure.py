"""Pre-close integration for initiative coordination workspaces."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

from .coordination_workspace import (
    CoordinationWorkspaceError,
    CoordinationWorkspaceStore,
)
from .journal import ExternalOperationJournal, JournalIntent, JournalRejected
from .workspace import (
    _GitWorkspaceExecutor,
    _TrustedRepositoryRegistry,
    _WorkspaceMember,
    _WorkspaceRejected,
)

__all__ = [
    "CoordinationClosureController",
    "CoordinationClosureError",
    "prepare_coordination_closure",
]


class CoordinationClosureError(Exception):
    """Raised when coordination members cannot be safely merged for closure."""


class CoordinationClosureController:
    """Merge every coordination member before formal initiative closure."""

    _SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")

    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: _TrustedRepositoryRegistry,
        executor: Optional[_GitWorkspaceExecutor] = None,
    ) -> None:
        if type(conn) is not sqlite3.Connection:
            raise CoordinationClosureError("conn must be a sqlite3.Connection")
        if not isinstance(registry, _TrustedRepositoryRegistry):
            raise CoordinationClosureError("registry must be _TrustedRepositoryRegistry")
        if executor is None:
            executor = _GitWorkspaceExecutor()
        if not isinstance(executor, _GitWorkspaceExecutor):
            raise CoordinationClosureError("executor must be _GitWorkspaceExecutor")

        self._conn = conn
        self._registry = registry
        self._executor = executor
        self._store = CoordinationWorkspaceStore(conn)
        self._journal = ExternalOperationJournal(
            conn, "initiative_coordination_operation_journal"
        )

    @classmethod
    def _is_sha(cls, value: Any) -> bool:
        return isinstance(value, str) and cls._SHA_RE.fullmatch(value) is not None

    def _journal_write(self, callback: Callable[[], Any]) -> Any:
        if self._conn.in_transaction:
            raise CoordinationClosureError("active transaction not allowed")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            result = callback()
            self._conn.execute("COMMIT")
            return result
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _build_members(
        self, workspace: dict[str, Any], initiative_id: str
    ) -> list[_WorkspaceMember]:
        workspace_id = workspace.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise CoordinationClosureError("workspace_id must be a nonblank string")
        rows = workspace.get("members")
        if not isinstance(rows, list) or not rows:
            raise CoordinationClosureError("workspace has no members")

        members: list[_WorkspaceMember] = []
        seen: set[str] = set()
        try:
            ordered = sorted(rows, key=lambda row: row["repository_identity"])
        except (KeyError, TypeError) as exc:
            raise CoordinationClosureError("workspace contains a malformed member") from exc

        for row in ordered:
            if not isinstance(row, dict):
                raise CoordinationClosureError("workspace contains a malformed member")
            repository_identity = row.get("repository_identity")
            if not isinstance(repository_identity, str) or not repository_identity.strip():
                raise CoordinationClosureError(
                    "repository_identity must be a nonblank string"
                )
            if repository_identity in seen:
                raise CoordinationClosureError(
                    f"duplicate repository_identity {repository_identity!r}"
                )
            seen.add(repository_identity)

            member_state = row.get("member_state")
            if member_state not in ("materialized", "merged"):
                raise CoordinationClosureError(
                    f"member {repository_identity!r} is not ready for closure"
                )
            expected_relative = (
                f"{initiative_id}/coordination/{repository_identity}"
            )
            expected_branch = (
                f"initiative/{initiative_id}/coordination/{repository_identity}"
            )
            if row.get("relative_path") != expected_relative:
                raise CoordinationClosureError(
                    f"member {repository_identity!r} relative path mismatch"
                )
            if row.get("branch") != expected_branch:
                raise CoordinationClosureError(
                    f"member {repository_identity!r} branch mismatch"
                )
            required_base_sha = row.get("required_base_sha")
            observed_head = row.get("observed_head")
            if not self._is_sha(required_base_sha):
                raise CoordinationClosureError(
                    f"member {repository_identity!r} has invalid required base"
                )
            if not self._is_sha(observed_head):
                raise CoordinationClosureError(
                    f"member {repository_identity!r} has invalid observed head"
                )

            try:
                registration = self._registry.lookup(repository_identity)
                controlled_root = registration.controlled_worktree_root
                target_path = os.path.normpath(
                    os.path.join(controlled_root, expected_relative)
                )
                Path(target_path).resolve().relative_to(Path(controlled_root).resolve())
                member = _WorkspaceMember(
                    workspace_id=workspace_id,
                    repository_identity=repository_identity,
                    repository_root=registration.repository_root,
                    controlled_worktree_root=controlled_root,
                    relative_path=expected_relative,
                    target_path=target_path,
                    branch=expected_branch,
                    required_base_sha=required_base_sha,
                    observed_head=observed_head,
                    member_state=member_state,
                )
            except (ValueError, _WorkspaceRejected) as exc:
                raise CoordinationClosureError(
                    f"member {repository_identity!r} is outside its trusted registration"
                ) from exc
            members.append(member)
        return members

    @staticmethod
    def _require_verified_merge(member: _WorkspaceMember, result: Any) -> str:
        if (
            not result.ready
            or result.source_head != member.observed_head
            or not result.source_contained
            or not result.remote_contained
            or not isinstance(result.merge_head, str)
            or not result.merge_head.strip()
            or not isinstance(result.remote_main_head, str)
            or not result.remote_main_head.strip()
        ):
            raise CoordinationClosureError(
                f"member {member.repository_identity!r} merge verification failed: "
                f"{result.failures}"
            )
        return result.merge_head

    def _preflight(self, members: list[_WorkspaceMember]) -> dict[str, str]:
        merge_heads: dict[str, str] = {}
        for member in members:
            try:
                status = self._executor._git(
                    member.target_path, "status", "--porcelain"
                )
                verification = self._executor.verify(member)
            except _WorkspaceRejected as exc:
                raise CoordinationClosureError(
                    f"member {member.repository_identity!r} preflight failed: {exc}"
                ) from exc
            if status.strip():
                raise CoordinationClosureError(
                    f"member {member.repository_identity!r} worktree is dirty"
                )
            if (
                not verification.ready
                or verification.observed_head != member.observed_head
            ):
                raise CoordinationClosureError(
                    f"member {member.repository_identity!r} evidence mismatch: "
                    f"{verification.failures}"
                )
            if member.member_state == "merged":
                try:
                    merged = self._executor.verify_merge(
                        member,
                        expected_main_sha=member.required_base_sha,
                        expected_source_head=member.observed_head,
                    )
                except _WorkspaceRejected as exc:
                    raise CoordinationClosureError(
                        f"member {member.repository_identity!r} merge preflight failed: {exc}"
                    ) from exc
                merge_heads[member.repository_identity] = self._require_verified_merge(
                    member, merged
                )
        return merge_heads

    def _append_failure(
        self,
        *,
        operation_id: str,
        member_target: str,
        error: Exception,
        actor_evidence: str,
        at: int,
    ) -> None:
        try:
            if self._journal.recovery_action(operation_id, member_target) != "verify":
                return
            self._journal_write(
                lambda: self._journal.append_failed(
                    operation_id=operation_id,
                    member_target=member_target,
                    observed_git_evidence=None,
                    observed_filesystem_evidence=None,
                    error_disposition=str(error) or type(error).__name__,
                    recovery_disposition="resume",
                    actor_evidence=actor_evidence,
                    created_at=at,
                )
            )
        except Exception:
            return

    def _merge_member(
        self,
        *,
        workspace_id: str,
        member: _WorkspaceMember,
        actor_evidence: str,
        at: int,
    ) -> str:
        repository_identity = member.repository_identity
        operation_id = (
            f"coord-merge-{workspace_id}-{repository_identity}-{member.observed_head}"
        )
        intended_git = (
            f"base={member.required_base_sha};source={member.observed_head}"
        )
        intent = JournalIntent(
            operation_id=operation_id,
            idempotency_id=operation_id,
            member_target=repository_identity,
            operation_kind="coordination_merge",
            workspace_id=workspace_id,
            repository_identity=repository_identity,
            intended_git_evidence=intended_git,
            intended_filesystem_evidence=f"target={member.target_path}",
            actor_evidence=actor_evidence,
            created_at=at,
        )
        try:
            action = self._journal.recovery_action(
                operation_id, repository_identity
            )
            if action == "prepare":
                self._journal_write(lambda: self._journal.append_prepared(intent))
            elif action == "resume":
                self._journal_write(
                    lambda: self._journal.append_resume_prepared(
                        operation_id=operation_id,
                        member_target=repository_identity,
                        actor_evidence=actor_evidence,
                        created_at=at,
                    )
                )
            elif action == "halt":
                raise CoordinationClosureError(
                    f"member {repository_identity!r} has a halted merge operation"
                )
            elif action not in ("verify", "consume_verified"):
                raise CoordinationClosureError(
                    f"member {repository_identity!r} has unknown recovery action {action!r}"
                )

            result = self._executor.merge_to_origin_main(
                member,
                expected_main_sha=member.required_base_sha,
                expected_source_head=member.observed_head,
            )
            merge_head = self._require_verified_merge(member, result)
            if action != "consume_verified":
                observed_git = (
                    f"base={member.required_base_sha};source={member.observed_head};"
                    f"merge={merge_head};remote={result.remote_main_head};"
                    "source_contained=true;remote_contained=true"
                )
                self._journal_write(
                    lambda: self._journal.append_verified(
                        operation_id=operation_id,
                        member_target=repository_identity,
                        observed_git_evidence=observed_git,
                        observed_filesystem_evidence=(
                            f"exists=true;target={member.target_path}"
                        ),
                        actor_evidence=actor_evidence,
                        created_at=at,
                    )
                )
            return merge_head
        except (
            CoordinationClosureError,
            JournalRejected,
            _WorkspaceRejected,
            sqlite3.Error,
        ) as exc:
            self._append_failure(
                operation_id=operation_id,
                member_target=repository_identity,
                error=exc,
                actor_evidence=actor_evidence,
                at=at,
            )
            if isinstance(exc, CoordinationClosureError):
                raise
            raise CoordinationClosureError(
                f"member {repository_identity!r} merge failed: {exc}"
            ) from exc

    def merge_all(
        self, *, initiative_id: str, actor_evidence: str, at: int
    ) -> dict[str, Any]:
        if self._conn.in_transaction:
            raise CoordinationClosureError("active transaction not allowed")
        if not isinstance(initiative_id, str) or not initiative_id.strip():
            raise CoordinationClosureError("initiative_id must be a nonblank string")
        if not isinstance(actor_evidence, str) or not actor_evidence.strip():
            raise CoordinationClosureError("actor_evidence must be a nonblank string")
        if isinstance(at, bool) or not isinstance(at, int) or at <= 0:
            raise CoordinationClosureError("at must be a positive integer")

        try:
            workspace = self._store.read_active(initiative_id)
        except CoordinationWorkspaceError as exc:
            raise CoordinationClosureError(
                f"failed to read coordination workspace: {exc}"
            ) from exc
        workspace_state = workspace.get("lifecycle_state")
        if workspace_state not in ("materialized", "merged"):
            raise CoordinationClosureError(
                f"workspace state {workspace_state!r} is not ready for closure"
            )
        workspace_id = workspace["workspace_id"]
        members = self._build_members(workspace, initiative_id)
        merge_heads = self._preflight(members)

        for member in members:
            if member.member_state == "merged":
                continue
            merge_head = self._merge_member(
                workspace_id=workspace_id,
                member=member,
                actor_evidence=actor_evidence,
                at=at,
            )
            merge_heads[member.repository_identity] = merge_head
            try:
                self._store.transition_member(
                    workspace_id,
                    member.repository_identity,
                    expected_state="materialized",
                    to_state="merged",
                    at=at,
                )
            except CoordinationWorkspaceError as exc:
                raise CoordinationClosureError(
                    f"failed to consume merged member {member.repository_identity!r}: {exc}"
                ) from exc

        if workspace_state == "materialized":
            try:
                self._store.transition_workspace(
                    workspace_id,
                    expected_state="materialized",
                    to_state="merged",
                    at=at,
                )
            except CoordinationWorkspaceError as exc:
                raise CoordinationClosureError(
                    f"failed to consume merged workspace: {exc}"
                ) from exc
        try:
            refreshed = self._store.read_active(initiative_id)
        except CoordinationWorkspaceError as exc:
            raise CoordinationClosureError(
                f"failed to read merged workspace: {exc}"
            ) from exc
        result = dict(refreshed)
        result["merge_heads"] = merge_heads
        return result


def prepare_coordination_closure(
    conn, registry, *, initiative_id, actor_evidence, at
):
    cursor = conn.execute(
        "SELECT workspace_id FROM initiative_coordination_workspaces "
        "WHERE initiative_id = ? AND active = 1",
        (initiative_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return CoordinationClosureController(conn, registry).merge_all(
        initiative_id=initiative_id,
        actor_evidence=actor_evidence,
        at=at,
    )

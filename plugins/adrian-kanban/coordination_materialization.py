"""Coordination materialization for initiative coordination workspaces."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .coordination_workspace import (
    CoordinationWorkspaceError,
    CoordinationWorkspaceStore,
)
from .journal import ExternalOperationJournal, JournalIntent, JournalRejected
from .workspace import (
    _GitWorkspaceExecutor,
    _SegmentWorkspaceController,
    _TrustedRepositoryRegistry,
    _WorkspaceMember,
    _WorkspaceRejected,
)

__all__ = ["CoordinationMaterializer", "CoordinationMaterializationError"]


class CoordinationMaterializationError(Exception):
    """Module-specific error for coordination materialization."""


class CoordinationMaterializer:
    """Materializes initiative coordination workspaces."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: _TrustedRepositoryRegistry,
        executor: Optional[_GitWorkspaceExecutor] = None,
    ) -> None:
        if type(conn) is not sqlite3.Connection:
            raise CoordinationMaterializationError("conn must be a sqlite3.Connection")
        if not isinstance(registry, _TrustedRepositoryRegistry):
            raise CoordinationMaterializationError("registry must be _TrustedRepositoryRegistry")
        if executor is None:
            executor = _GitWorkspaceExecutor()
        if not isinstance(executor, _GitWorkspaceExecutor):
            raise CoordinationMaterializationError("executor must be _GitWorkspaceExecutor")
        self._conn = conn
        self._registry = registry
        self._executor = executor
        self._store = CoordinationWorkspaceStore(conn)
        self._journal = ExternalOperationJournal(conn, "initiative_coordination_operation_journal")

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def materialize(self, *, initiative_id: str, actor_evidence: str, at: int) -> dict:
        if self._conn.in_transaction:
            raise CoordinationMaterializationError("active transaction not allowed")
        if not isinstance(initiative_id, str) or not initiative_id.strip():
            raise CoordinationMaterializationError("initiative_id must be a nonblank string")
        if not isinstance(actor_evidence, str) or not actor_evidence.strip():
            raise CoordinationMaterializationError("actor_evidence must be a nonblank string")
        if isinstance(at, bool) or not isinstance(at, int) or at <= 0:
            raise CoordinationMaterializationError("at must be a positive integer")

        try:
            workspace = self._store.read_active(initiative_id)
        except CoordinationWorkspaceError as exc:
            raise CoordinationMaterializationError(f"failed to read active workspace: {exc}") from exc

        ws_state: str = workspace["lifecycle_state"]
        if ws_state not in ("planned", "materializing", "failed", "materialized"):
            raise CoordinationMaterializationError(
                f"workspace state {ws_state!r} is not materializable"
            )

        workspace_id: str = workspace["workspace_id"]
        members: List[Dict[str, Any]] = workspace["members"]
        if not members:
            raise CoordinationMaterializationError("workspace has no members")

        # If already materialized, verify every member physically and return.
        if ws_state == "materialized":
            member_roots: List[str] = []
            for member in sorted(members, key=lambda m: m["repository_identity"]):
                ws_member = self._build_workspace_member(
                    workspace_id, initiative_id, member
                )
                verification = self._executor.verify(ws_member)
                if (
                    not verification.ready
                    or verification.observed_head != member["observed_head"]
                ):
                    raise CoordinationMaterializationError(
                        f"materialized member {member['repository_identity']} "
                        f"evidence mismatch: ready={verification.ready}, "
                        f"observed_head={verification.observed_head}, "
                        f"stored={member['observed_head']!r}, "
                        f"failures={verification.failures}"
                    )
                member_roots.append(ws_member.target_path)
            result = dict(workspace)
            result["member_roots"] = member_roots
            return result

        # Transition planned/failed -> materializing.
        if ws_state in ("planned", "failed"):
            try:
                self._store.transition_workspace(
                    workspace_id,
                    expected_state=ws_state,
                    to_state="materializing",
                    at=at,
                )
            except CoordinationWorkspaceError as exc:
                raise CoordinationMaterializationError(
                    f"failed to transition workspace to materializing: {exc}"
                ) from exc

        # Process every member in sorted repository-identity order.
        member_roots = []
        for member in sorted(members, key=lambda m: m["repository_identity"]):
            repository_identity: str = member["repository_identity"]
            member_state: str = member["member_state"]

            if member_state in ("merged", "retired"):
                raise CoordinationMaterializationError(
                    f"member {repository_identity} is in state {member_state!r}, not materializable"
                )

            # Resolve registration and verify stored path/branch.
            try:
                registration = self._registry.lookup(repository_identity)
            except _WorkspaceRejected as exc:
                raise CoordinationMaterializationError(
                    f"repository {repository_identity!r} not found in trusted registry: {exc}"
                ) from exc

            expected_relative_path = f"{initiative_id}/coordination/{repository_identity}"
            expected_branch = f"initiative/{initiative_id}/coordination/{repository_identity}"
            if member["relative_path"] != expected_relative_path:
                raise CoordinationMaterializationError(
                    f"member {repository_identity} relative path mismatch: "
                    f"stored {member['relative_path']!r}, expected {expected_relative_path!r}"
                )
            if member["branch"] != expected_branch:
                raise CoordinationMaterializationError(
                    f"member {repository_identity} branch mismatch: "
                    f"stored {member['branch']!r}, expected {expected_branch!r}"
                )

            # Build target path and verify containment.
            controlled_root = registration.controlled_worktree_root
            target_path = os.path.normpath(os.path.join(controlled_root, expected_relative_path))
            try:
                Path(target_path).resolve().relative_to(Path(controlled_root).resolve())
            except ValueError:
                raise CoordinationMaterializationError(
                    f"member {repository_identity} target path resolves outside controlled root"
                )

            # Build the _WorkspaceMember DTO.
            required_base_sha: Optional[str] = member["required_base_sha"]
            observed_head: Optional[str] = member["observed_head"]

            # For planned members, pin the base first.
            if member_state == "planned":
                try:
                    trusted_sha = _SegmentWorkspaceController._resolve_origin_main(
                        registration.repository_root
                    )
                except _WorkspaceRejected as exc:
                    raise CoordinationMaterializationError(
                        f"failed to resolve origin/main for {repository_identity}: {exc}"
                    ) from exc
                try:
                    self._store.pin_member_base(
                        workspace_id,
                        repository_identity,
                        trusted_sha,
                        at,
                    )
                except CoordinationWorkspaceError as exc:
                    raise CoordinationMaterializationError(
                        f"failed to pin member base for {repository_identity}: {exc}"
                    ) from exc
                required_base_sha = trusted_sha

            # Transition member state as needed.
            if member_state == "planned":
                try:
                    self._store.transition_member(
                        workspace_id,
                        repository_identity,
                        expected_state="planned",
                        to_state="materializing",
                        at=at,
                    )
                except CoordinationWorkspaceError as exc:
                    raise CoordinationMaterializationError(
                        f"failed to transition member {repository_identity} to materializing: {exc}"
                    ) from exc
            elif member_state == "failed":
                try:
                    self._store.transition_member(
                        workspace_id,
                        repository_identity,
                        expected_state="failed",
                        to_state="materializing",
                        at=at,
                    )
                except CoordinationWorkspaceError as exc:
                    raise CoordinationMaterializationError(
                        f"failed to transition member {repository_identity} to materializing: {exc}"
                    ) from exc
            elif member_state == "materializing":
                pass  # crash-recovery continuation
            elif member_state == "materialized":
                # Physically verify and skip replay.
                ws_member = _WorkspaceMember(
                    workspace_id=workspace_id,
                    repository_identity=repository_identity,
                    repository_root=registration.repository_root,
                    controlled_worktree_root=controlled_root,
                    relative_path=expected_relative_path,
                    target_path=target_path,
                    branch=expected_branch,
                    required_base_sha=required_base_sha,
                    observed_head=observed_head,
                    member_state="materialized",
                )
                verification = self._executor.verify(ws_member)
                if (
                    not verification.ready
                    or verification.observed_head != member["observed_head"]
                ):
                    raise CoordinationMaterializationError(
                        f"materialized member {repository_identity} evidence mismatch: "
                        f"ready={verification.ready}, "
                        f"observed_head={verification.observed_head}, "
                        f"stored={member['observed_head']!r}, "
                        f"failures={verification.failures}"
                    )
                member_roots.append(target_path)
                continue
            else:
                raise CoordinationMaterializationError(
                    f"member {repository_identity} in unexpected state {member_state!r}"
                )

            # Build the _WorkspaceMember DTO for the executor.
            ws_member = _WorkspaceMember(
                workspace_id=workspace_id,
                repository_identity=repository_identity,
                repository_root=registration.repository_root,
                controlled_worktree_root=controlled_root,
                relative_path=expected_relative_path,
                target_path=target_path,
                branch=expected_branch,
                required_base_sha=required_base_sha,
                observed_head=observed_head,
                member_state="planned",
            )

            # Stable operation identity.
            operation_id = f"coord-materialize-{workspace_id}-{repository_identity}"
            idempotency_id = operation_id

            # Intended evidence.
            intended_git_evidence = f"base={required_base_sha};branch={expected_branch}"
            intended_filesystem_evidence = f"target={target_path}"

            intent = JournalIntent(
                operation_id=operation_id,
                idempotency_id=idempotency_id,
                member_target=repository_identity,
                operation_kind="coordination_materialize",
                workspace_id=workspace_id,
                repository_identity=repository_identity,
                intended_git_evidence=intended_git_evidence,
                intended_filesystem_evidence=intended_filesystem_evidence,
                actor_evidence=actor_evidence,
                created_at=at,
            )

            # Determine recovery action and execute.
            try:
                action = self._journal.recovery_action(operation_id, repository_identity)
            except JournalRejected as exc:
                raise CoordinationMaterializationError(
                    f"journal recovery action failed for {repository_identity}: {exc}"
                ) from exc

            if action == "prepare":
                self._journal_append_prepared(intent)
                action = "verify"
            elif action == "resume":
                self._journal_append_resume_prepared(intent, at)
                action = "verify"
            elif action == "verify":
                pass
            elif action == "consume_verified":
                self._consume_verified(intent, ws_member, at)
                member_roots.append(target_path)
                continue
            elif action == "halt":
                raise CoordinationMaterializationError(
                    f"operation for {repository_identity} is halted; manual intervention required"
                )
            else:
                raise CoordinationMaterializationError(
                    f"unknown recovery action {action!r} for {repository_identity}"
                )

            # Execute verify/materialize/verify sequence.
            try:
                verification = self._executor.verify(ws_member)
            except _WorkspaceRejected as exc:
                self._mark_member_failed(workspace_id, repository_identity, at, str(exc))
                raise CoordinationMaterializationError(
                    f"verification failed for {repository_identity}: {exc}"
                ) from exc

            if verification.ready:
                self._journal_append_verified(intent, ws_member, verification, at)
            elif verification.failures == ("member_absent",):
                try:
                    self._executor.materialize(ws_member)
                except _WorkspaceRejected:
                    pass
                try:
                    verification = self._executor.verify(ws_member)
                except _WorkspaceRejected as exc:
                    self._mark_member_failed(workspace_id, repository_identity, at, str(exc))
                    raise CoordinationMaterializationError(
                        f"re-verification failed for {repository_identity}: {exc}"
                    ) from exc
                if verification.ready:
                    self._journal_append_verified(intent, ws_member, verification, at)
                elif verification.failures == ("member_absent",):
                    self._journal_append_failed(
                        intent,
                        ws_member,
                        verification,
                        at,
                        error_disposition="effect absent after verification",
                        recovery_disposition="resume",
                    )
                    self._mark_member_failed(
                        workspace_id,
                        repository_identity,
                        at,
                        "materialization effect absent after verification",
                    )
                    raise CoordinationMaterializationError(
                        f"materialization failed for {repository_identity}: "
                        f"effect absent after verification"
                    )
                else:
                    self._journal_append_failed(
                        intent,
                        ws_member,
                        verification,
                        at,
                        error_disposition="unsafe workspace state after verification",
                        recovery_disposition="manual intervention required",
                    )
                    self._mark_member_failed(
                        workspace_id,
                        repository_identity,
                        at,
                        f"unsafe state: {verification.failures}",
                    )
                    raise CoordinationMaterializationError(
                        f"materialization failed for {repository_identity}: "
                        f"unsafe state {verification.failures}"
                    )
            else:
                self._journal_append_failed(
                    intent,
                    ws_member,
                    verification,
                    at,
                    error_disposition="unsafe workspace state after verification",
                    recovery_disposition="manual intervention required",
                )
                self._mark_member_failed(
                    workspace_id,
                    repository_identity,
                    at,
                    f"unsafe state: {verification.failures}",
                )
                raise CoordinationMaterializationError(
                    f"materialization failed for {repository_identity}: "
                    f"unsafe state {verification.failures}"
                )

            # Consume verified evidence.
            self._consume_verified(intent, ws_member, at)
            member_roots.append(target_path)

        # Transition workspace materializing -> materialized.
        try:
            self._store.transition_workspace(
                workspace_id,
                expected_state="materializing",
                to_state="materialized",
                at=at,
            )
        except CoordinationWorkspaceError as exc:
            raise CoordinationMaterializationError(
                f"failed to transition workspace to materialized: {exc}"
            ) from exc

        try:
            result = self._store.read_active(initiative_id)
        except CoordinationWorkspaceError as exc:
            raise CoordinationMaterializationError(f"failed to read active workspace: {exc}") from exc
        result["member_roots"] = member_roots
        return result

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _build_workspace_member(
        self,
        workspace_id: str,
        initiative_id: str,
        member: Dict[str, Any],
    ) -> _WorkspaceMember:
        repository_identity: str = member["repository_identity"]
        if member["member_state"] != "materialized":
            raise CoordinationMaterializationError(
                f"member {repository_identity} is not materialized"
            )
        if member["required_base_sha"] is None or member["observed_head"] is None:
            raise CoordinationMaterializationError(
                f"member {repository_identity} is missing required base or observed head"
            )
        try:
            registration = self._registry.lookup(repository_identity)
        except _WorkspaceRejected as exc:
            raise CoordinationMaterializationError(
                f"repository {repository_identity!r} not found in trusted registry: {exc}"
            ) from exc
        expected_relative_path = (
            f"{initiative_id}/coordination/{repository_identity}"
        )
        expected_branch = (
            f"initiative/{initiative_id}/coordination/{repository_identity}"
        )
        if member["relative_path"] != expected_relative_path:
            raise CoordinationMaterializationError(
                f"member {repository_identity} relative path mismatch: "
                f"stored {member['relative_path']!r}, "
                f"expected {expected_relative_path!r}"
            )
        if member["branch"] != expected_branch:
            raise CoordinationMaterializationError(
                f"member {repository_identity} branch mismatch: "
                f"stored {member['branch']!r}, expected {expected_branch!r}"
            )
        controlled_root = registration.controlled_worktree_root
        target_path = os.path.normpath(
            os.path.join(controlled_root, expected_relative_path)
        )
        try:
            Path(target_path).resolve().relative_to(Path(controlled_root).resolve())
        except ValueError:
            raise CoordinationMaterializationError(
                f"member {repository_identity} target path resolves outside controlled root"
            )
        return _WorkspaceMember(
            workspace_id=workspace_id,
            repository_identity=repository_identity,
            repository_root=registration.repository_root,
            controlled_worktree_root=controlled_root,
            relative_path=expected_relative_path,
            target_path=target_path,
            branch=expected_branch,
            required_base_sha=member["required_base_sha"],
            observed_head=member["observed_head"],
            member_state=member["member_state"],
        )

    def _owned_transaction(self, callback) -> Any:
        if self._conn.in_transaction:
            raise CoordinationMaterializationError("active transaction not allowed")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            result = callback()
            self._conn.commit()
            return result
        except BaseException as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            if isinstance(exc, CoordinationMaterializationError):
                raise
            if isinstance(exc, (JournalRejected, sqlite3.Error, CoordinationWorkspaceError)):
                raise CoordinationMaterializationError(str(exc)) from exc
            raise CoordinationMaterializationError(str(exc)) from exc

    def _journal_append_prepared(self, intent: JournalIntent) -> None:
        def _append():
            self._journal.append_prepared(intent)
        self._owned_transaction(_append)

    def _journal_append_resume_prepared(self, intent: JournalIntent, at: int) -> None:
        def _append():
            self._journal.append_resume_prepared(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        self._owned_transaction(_append)

    def _journal_append_verified(
        self,
        intent: JournalIntent,
        member: _WorkspaceMember,
        verification,
        at: int,
    ) -> None:
        observed_git = (
            f"head={verification.observed_head};branch={member.branch};"
            f"base_contained=true;common={verification.common_repository}"
        )
        observed_fs = f"exists=true;target={member.target_path}"

        def _append():
            self._journal.append_verified(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=observed_git,
                observed_filesystem_evidence=observed_fs,
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        self._owned_transaction(_append)

    def _journal_append_failed(
        self,
        intent: JournalIntent,
        member: _WorkspaceMember,
        verification,
        at: int,
        *,
        error_disposition: str,
        recovery_disposition: str,
    ) -> None:
        observed_git = f"head={verification.observed_head}" if verification.observed_head else None
        observed_fs = (
            f"exists={'true' if os.path.exists(member.target_path) else 'false'};"
            f"target={member.target_path}"
        )

        def _append():
            self._journal.append_failed(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=observed_git,
                observed_filesystem_evidence=observed_fs,
                error_disposition=error_disposition,
                recovery_disposition=recovery_disposition,
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        self._owned_transaction(_append)

    def _consume_verified(
        self, intent: JournalIntent, member: _WorkspaceMember, at: int
    ) -> None:
        # Re-verify physically.
        verification = self._executor.verify(member)
        if not verification.ready:
            raise CoordinationMaterializationError(
                f"consume verification failed for {member.repository_identity}: "
                f"{verification.failures}"
            )

        # Require exact stored observed evidence.
        evidence = self._journal.verified_evidence(intent.operation_id, intent.member_target)
        if evidence is None:
            raise CoordinationMaterializationError(
                f"no verified journal evidence for {member.repository_identity}"
            )

        expected_git = (
            f"head={verification.observed_head};branch={member.branch};"
            f"base_contained=true;common={verification.common_repository}"
        )
        expected_fs = f"exists=true;target={member.target_path}"
        if (
            evidence.observed_git_evidence != expected_git
            or evidence.observed_filesystem_evidence != expected_fs
        ):
            raise CoordinationMaterializationError(
                f"verified evidence mismatch for {member.repository_identity}"
            )

        # Database consumption.
        try:
            self._store.consume_materialization(
                member.workspace_id,
                member.repository_identity,
                verification.observed_head,
                at,
            )
        except CoordinationWorkspaceError as exc:
            raise CoordinationMaterializationError(
                f"failed to consume materialization for {member.repository_identity}: {exc}"
            ) from exc

    def _mark_member_failed(
        self,
        workspace_id: str,
        repository_identity: str,
        at: int,
        detail: str,
    ) -> None:
        try:
            self._store.transition_member(
                workspace_id,
                repository_identity,
                expected_state="materializing",
                to_state="failed",
                at=at,
                failure_detail=detail,
            )
        except CoordinationWorkspaceError:
            pass
        try:
            self._store.transition_workspace(
                workspace_id,
                expected_state="materializing",
                to_state="failed",
                at=at,
                failure_detail=detail,
            )
        except CoordinationWorkspaceError:
            pass

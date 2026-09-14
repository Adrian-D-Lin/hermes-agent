"""Repository-freshness reconciliation for coordination workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from .coordination_materialization import CoordinationMaterializer
from .coordination_workspace import (
    CoordinationWorkspaceError,
    CoordinationWorkspaceStore,
)
from .journal import ExternalOperationJournal, JournalIntent, JournalRejected
from .workspace import (
    _GitWorkspaceExecutor,
    _MemberFreshness,
    _TrustedRepositoryRegistry,
    _WorkspaceMember,
    _WorkspaceRejected,
)

__all__ = ["CoordinationFreshnessController", "CoordinationFreshnessError"]


class CoordinationFreshnessError(Exception):
    """Fail-closed coordination-freshness error."""


class CoordinationFreshnessController:
    """Reconcile physical Git state with one materialized logical workspace."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: _TrustedRepositoryRegistry,
        executor: Optional[_GitWorkspaceExecutor] = None,
        advancement_confirmer=None,
    ) -> None:
        if type(conn) is not sqlite3.Connection:
            raise CoordinationFreshnessError("conn must be a sqlite3.Connection")
        if not isinstance(registry, _TrustedRepositoryRegistry):
            raise CoordinationFreshnessError(
                "registry must be _TrustedRepositoryRegistry"
            )
        if executor is None:
            executor = _GitWorkspaceExecutor()
        if not isinstance(executor, _GitWorkspaceExecutor):
            raise CoordinationFreshnessError(
                "executor must be _GitWorkspaceExecutor"
            )
        if advancement_confirmer is not None and not callable(
            advancement_confirmer
        ):
            raise CoordinationFreshnessError(
                "advancement_confirmer must be callable when provided"
            )
        self._conn = conn
        self._executor = executor
        self._store = CoordinationWorkspaceStore(conn)
        self._journal = ExternalOperationJournal(
            conn, "initiative_coordination_operation_journal"
        )
        self._materializer = CoordinationMaterializer(conn, registry, executor)
        self._advancement_confirmer = advancement_confirmer

    def reconcile(
        self, *, initiative_id: str, actor_evidence: str, at: int
    ) -> dict:
        if self._conn.in_transaction:
            raise CoordinationFreshnessError("active transaction not allowed")
        if not isinstance(initiative_id, str) or not initiative_id.strip():
            raise CoordinationFreshnessError(
                "initiative_id must be a nonblank string"
            )
        if not isinstance(actor_evidence, str) or not actor_evidence.strip():
            raise CoordinationFreshnessError(
                "actor_evidence must be a nonblank string"
            )
        if isinstance(at, bool) or not isinstance(at, int) or at <= 0:
            raise CoordinationFreshnessError("at must be a positive integer")

        try:
            workspace = self._store.read_active(initiative_id)
        except CoordinationWorkspaceError as exc:
            raise CoordinationFreshnessError(
                f"failed to read active workspace: {exc}"
            ) from exc
        if workspace["lifecycle_state"] != "materialized":
            raise CoordinationFreshnessError(
                f"workspace state {workspace['lifecycle_state']!r} is not materialized"
            )
        members: List[Dict[str, Any]] = workspace["members"]
        if not members:
            raise CoordinationFreshnessError("workspace has no members")
        workspace_id: str = workspace["workspace_id"]

        ws_members: List[_WorkspaceMember] = []
        for member in sorted(members, key=lambda item: item["repository_identity"]):
            try:
                ws_member = self._materializer._build_workspace_member(
                    workspace_id, initiative_id, member
                )
            except Exception as exc:
                raise CoordinationFreshnessError(
                    "failed to build workspace member for "
                    f"{member['repository_identity']}: {exc}"
                ) from exc
            ws_members.append(ws_member)

        freshness_results: List[_MemberFreshness] = []
        for ws_member in ws_members:
            try:
                freshness = self._executor.inspect_freshness(ws_member)
            except _WorkspaceRejected as exc:
                raise CoordinationFreshnessError(
                    "freshness inspection failed for "
                    f"{ws_member.repository_identity}: {exc}"
                ) from exc
            # A dirty worktree is expected while an admitted phase is being
            # worked.  It is evidence to surface, not a reason to revoke an
            # already valid session binding.  Every other freshness failure
            # remains blocking here; cleanliness is enforced at the phase
            # transition/merge/closure boundary.
            blocking_failures = tuple(
                failure
                for failure in freshness.failures
                if failure != "worktree_dirty"
            )
            if blocking_failures or (
                not freshness.ready
                and freshness.failures != ("worktree_dirty",)
            ):
                raise CoordinationFreshnessError(
                    f"member {ws_member.repository_identity} is not ready: "
                    f"{freshness.failures}"
                )
            freshness_results.append(freshness)

        classifications: List[Dict[str, Any]] = []
        unexplained: List[Dict[str, Any]] = []
        for ws_member, freshness in zip(ws_members, freshness_results):
            recorded = freshness.recorded_head
            local = freshness.local_head
            remote = freshness.remote_head
            if local is None or remote is None:
                raise CoordinationFreshnessError(
                    f"member {ws_member.repository_identity} has missing local or remote head"
                )
            if recorded != local and not freshness.recorded_is_ancestor_of_local:
                raise CoordinationFreshnessError(
                    f"member {ws_member.repository_identity}: recorded head "
                    f"{recorded} is not equal to or an ancestor of local head "
                    f"{local}; unexplained replacement/regression"
                )

            if freshness.local_is_ancestor_of_remote:
                category = "tracker_refresh" if local == remote else "remote_only_ff"
            elif freshness.remote_is_ancestor_of_local:
                if local == recorded:
                    category = "already_recorded"
                else:
                    category = "local_advancement"
                    if freshness.clean_including_untracked:
                        unexplained.append(
                            {
                                "repository_identity": ws_member.repository_identity,
                                "recorded_head": recorded,
                                "local_head": local,
                                "remote_head": remote,
                            }
                        )
            else:
                raise CoordinationFreshnessError(
                    f"member {ws_member.repository_identity}: genuine divergence "
                    f"between local {local} and remote {remote}"
                )
            classifications.append(
                {
                    "ws_member": ws_member,
                    "category": category,
                    "dirty": not freshness.clean_including_untracked,
                    "recorded": recorded,
                    "local": local,
                    "remote": remote,
                }
            )

        if unexplained:
            if self._advancement_confirmer is None:
                raise CoordinationFreshnessError(
                    "unexplained local advancement detected but no "
                    "advancement_confirmer was provided; declining fail-closed"
                )
            try:
                confirmed = self._advancement_confirmer(tuple(unexplained))
            except Exception as exc:
                raise CoordinationFreshnessError(
                    f"advancement confirmer raised: {exc}"
                ) from exc
            if confirmed is not True:
                raise CoordinationFreshnessError(
                    "advancement confirmer declined; refusing to proceed"
                )

        actions: List[Dict[str, Any]] = []
        for classification in classifications:
            ws_member = classification["ws_member"]
            category = classification["category"]
            dirty = classification["dirty"]
            recorded = classification["recorded"]
            local = classification["local"]
            remote = classification["remote"]
            repository_identity = ws_member.repository_identity
            current_base = ws_member.required_base_sha

            if dirty:
                # Never mutate Git or advance Tracker head/base evidence while
                # uncommitted work exists.  A later clean revalidation can
                # reconcile it safely; the transition gate will reject until
                # then.
                actions.append(
                    {
                        "repository_identity": repository_identity,
                        "action": "dirty_observed",
                        "recorded_head": recorded,
                        "local_head": local,
                        "remote_head": remote,
                    }
                )
                continue

            if category == "remote_only_ff":
                self._perform_fast_forward(
                    workspace_id=workspace_id,
                    ws_member=ws_member,
                    recorded=recorded,
                    local=local,
                    remote=remote,
                    actor_evidence=actor_evidence,
                    at=at,
                )
                self._replace_freshness(
                    workspace_id,
                    ws_member,
                    recorded=recorded,
                    current_base=current_base,
                    new_head=remote,
                    new_base=remote,
                    at=at,
                )
                actions.append(
                    {
                        "repository_identity": repository_identity,
                        "action": "fast_forward",
                        "from_head": local,
                        "to_head": remote,
                    }
                )
            elif category == "tracker_refresh":
                changed = recorded != local or current_base != remote
                if changed:
                    self._handle_tracker_refresh(
                        workspace_id=workspace_id,
                        ws_member=ws_member,
                        recorded=recorded,
                        local=local,
                        remote=remote,
                        actor_evidence=actor_evidence,
                        at=at,
                    )
                    actions.append(
                        {
                            "repository_identity": repository_identity,
                            "action": "tracker_refresh",
                            "head": local,
                        }
                    )
            elif category == "already_recorded":
                if current_base != remote:
                    self._replace_freshness(
                        workspace_id,
                        ws_member,
                        recorded=recorded,
                        current_base=current_base,
                        new_head=recorded,
                        new_base=remote,
                        at=at,
                    )
                    actions.append(
                        {
                            "repository_identity": repository_identity,
                            "action": "base_refresh",
                            "head": recorded,
                            "new_base": remote,
                        }
                    )
            elif category == "local_advancement":
                self._replace_freshness(
                    workspace_id,
                    ws_member,
                    recorded=recorded,
                    current_base=current_base,
                    new_head=local,
                    new_base=remote,
                    at=at,
                )
                actions.append(
                    {
                        "repository_identity": repository_identity,
                        "action": "local_advancement",
                        "head": local,
                        "new_base": remote,
                    }
                )

        try:
            result = self._store.read_active(initiative_id)
        except CoordinationWorkspaceError as exc:
            raise CoordinationFreshnessError(
                f"failed to re-read workspace: {exc}"
            ) from exc
        member_roots = []
        for member in sorted(
            result["members"], key=lambda item: item["repository_identity"]
        ):
            try:
                rebuilt = self._materializer._build_workspace_member(
                    result["workspace_id"], initiative_id, member
                )
            except Exception as exc:
                raise CoordinationFreshnessError(
                    "failed to rebuild workspace member for "
                    f"{member['repository_identity']}: {exc}"
                ) from exc
            member_roots.append(rebuilt.target_path)
        result["member_roots"] = member_roots
        result["freshness_actions"] = actions
        return result

    def _replace_freshness(
        self,
        workspace_id: str,
        ws_member: _WorkspaceMember,
        *,
        recorded: str,
        current_base: str,
        new_head: str,
        new_base: str,
        at: int,
    ) -> None:
        try:
            self._store.replace_member_freshness(
                workspace_id,
                ws_member.repository_identity,
                expected_observed_head=recorded,
                expected_required_base_sha=current_base,
                new_observed_head=new_head,
                new_required_base_sha=new_base,
                at=at,
            )
        except CoordinationWorkspaceError as exc:
            raise CoordinationFreshnessError(
                "failed to update freshness for "
                f"{ws_member.repository_identity}: {exc}"
            ) from exc

    def _perform_fast_forward(
        self,
        *,
        workspace_id: str,
        ws_member: _WorkspaceMember,
        recorded: str,
        local: str,
        remote: str,
        actor_evidence: str,
        at: int,
    ) -> None:
        repository_identity = ws_member.repository_identity
        operation_id = self._derive_operation_id(
            workspace_id, repository_identity, recorded, remote
        )
        intent = JournalIntent(
            operation_id=operation_id,
            idempotency_id=operation_id,
            member_target=repository_identity,
            operation_kind="coordination_freshness_ff",
            workspace_id=workspace_id,
            repository_identity=repository_identity,
            intended_git_evidence=self._canonical_git_evidence(
                branch=ws_member.branch,
                local=local,
                recorded=recorded,
                remote=remote,
            ),
            intended_filesystem_evidence=f"target={ws_member.target_path}",
            actor_evidence=actor_evidence,
            created_at=at,
        )
        try:
            action = self._journal.recovery_action(
                operation_id, repository_identity
            )
        except JournalRejected as exc:
            raise CoordinationFreshnessError(
                "journal recovery action failed for "
                f"{repository_identity}: {exc}"
            ) from exc

        if action == "prepare":
            self._journal_append_prepared(intent)
        elif action == "resume":
            self._journal_append_resume_prepared(intent, at)
        elif action == "consume_verified":
            self._consume_verified_ff(ws_member, recorded, remote)
            return
        elif action == "halt":
            raise CoordinationFreshnessError(
                f"operation for {repository_identity} is halted; "
                "manual intervention required"
            )
        elif action != "verify":
            raise CoordinationFreshnessError(
                f"unknown recovery action {action!r} for {repository_identity}"
            )

        stored_intent = self._journal.head(operation_id, repository_identity)
        if stored_intent is None or stored_intent.state != "prepared":
            raise CoordinationFreshnessError(
                f"journal head for {repository_identity} is not prepared"
            )
        stored = self._parse_git_evidence(stored_intent.intended_git_evidence)
        if (
            stored["branch"] != ws_member.branch
            or stored["recorded"] != recorded
            or stored["remote"] != remote
        ):
            raise CoordinationFreshnessError(
                f"journal intent mismatch for {repository_identity}"
            )

        try:
            current_head = self._executor._git(
                ws_member.target_path, "rev-parse", "HEAD"
            )
        except _WorkspaceRejected as exc:
            raise CoordinationFreshnessError(
                f"failed to read physical HEAD for {repository_identity}: {exc}"
            ) from exc

        if current_head == stored["local"]:
            try:
                self._executor.fast_forward_to_remote(
                    ws_member,
                    expected_local_head=stored["local"],
                    expected_remote_head=remote,
                )
            except _WorkspaceRejected as exc:
                self._record_ff_failure(
                    intent, ws_member, stored["local"], remote, at, exc
                )
            self._journal_append_ff_verified(
                intent, ws_member, stored["local"], remote, at
            )
            return
        if current_head == remote:
            self._verify_ff_physical(ws_member, remote)
            self._journal_append_ff_verified(
                intent, ws_member, stored["local"], remote, at
            )
            return

        self._journal_append_failed(
            intent,
            ws_member,
            at,
            error_disposition="unsafe workspace state during fast-forward recovery",
            recovery_disposition="manual intervention required",
        )
        raise CoordinationFreshnessError(
            f"fast-forward recovery for {repository_identity} found physical "
            f"HEAD {current_head!r}, expected {stored['local']!r} or {remote!r}"
        )

    def _record_ff_failure(
        self,
        intent: JournalIntent,
        ws_member: _WorkspaceMember,
        original_local: str,
        remote: str,
        at: int,
        exc: Exception,
    ) -> None:
        try:
            post_head = self._executor._git(
                ws_member.target_path, "rev-parse", "HEAD"
            )
        except _WorkspaceRejected as read_exc:
            raise CoordinationFreshnessError(
                "failed to read physical HEAD after fast-forward failure for "
                f"{ws_member.repository_identity}: {read_exc}"
            ) from read_exc
        if post_head == original_local:
            recovery = "resume"
            disposition = "fast-forward failed"
        else:
            recovery = "manual intervention required"
            disposition = "unsafe workspace state after fast-forward failure"
        self._journal_append_failed(
            intent,
            ws_member,
            at,
            error_disposition=disposition,
            recovery_disposition=recovery,
        )
        raise CoordinationFreshnessError(
            f"fast-forward failed for {ws_member.repository_identity}: {exc}; "
            f"physical HEAD is {post_head!r}, expected {original_local!r} or {remote!r}"
        ) from exc

    def _handle_tracker_refresh(
        self,
        *,
        workspace_id: str,
        ws_member: _WorkspaceMember,
        recorded: str,
        local: str,
        remote: str,
        actor_evidence: str,
        at: int,
    ) -> None:
        operation_id = self._derive_operation_id(
            workspace_id, ws_member.repository_identity, recorded, remote
        )
        try:
            journal_head = self._journal.head(
                operation_id, ws_member.repository_identity
            )
        except JournalRejected as exc:
            raise CoordinationFreshnessError(
                "failed to check journal head for "
                f"{ws_member.repository_identity}: {exc}"
            ) from exc
        if journal_head is not None:
            self._perform_fast_forward(
                workspace_id=workspace_id,
                ws_member=ws_member,
                recorded=recorded,
                local=local,
                remote=remote,
                actor_evidence=actor_evidence,
                at=at,
            )
        self._replace_freshness(
            workspace_id,
            ws_member,
            recorded=recorded,
            current_base=ws_member.required_base_sha,
            new_head=local,
            new_base=remote,
            at=at,
        )

    def _consume_verified_ff(
        self,
        ws_member: _WorkspaceMember,
        recorded: str,
        remote: str,
    ) -> None:
        operation_id = self._derive_operation_id(
            ws_member.workspace_id,
            ws_member.repository_identity,
            recorded,
            remote,
        )
        head = self._journal.head(operation_id, ws_member.repository_identity)
        if head is None or head.state != "verified":
            raise CoordinationFreshnessError(
                f"no verified journal evidence for {ws_member.repository_identity}"
            )
        stored = self._parse_git_evidence(head.intended_git_evidence)
        if (
            stored["branch"] != ws_member.branch
            or stored["recorded"] != recorded
            or stored["remote"] != remote
        ):
            raise CoordinationFreshnessError(
                "journal intent mismatch for "
                f"{ws_member.repository_identity} during consume"
            )
        expected_git = (
            f"local={stored['local']};remote={remote};"
            f"branch={ws_member.branch};ff=true"
        )
        expected_fs = f"exists=true;target={ws_member.target_path}"
        if (
            head.observed_git_evidence != expected_git
            or head.observed_filesystem_evidence != expected_fs
        ):
            raise CoordinationFreshnessError(
                "verified evidence mismatch for "
                f"{ws_member.repository_identity}"
            )
        self._verify_ff_physical(ws_member, remote)

    def _verify_ff_physical(
        self, ws_member: _WorkspaceMember, expected_remote: str
    ) -> None:
        repository_identity = ws_member.repository_identity
        try:
            current_head = self._executor._git(
                ws_member.target_path, "rev-parse", "HEAD"
            )
            branch = self._executor._git(
                ws_member.target_path, "symbolic-ref", "--short", "HEAD"
            )
            status = self._executor._git(
                ws_member.target_path,
                "status",
                "--porcelain",
                "--untracked-files=all",
            )
        except _WorkspaceRejected as exc:
            raise CoordinationFreshnessError(
                f"failed to verify physical state for {repository_identity}: {exc}"
            ) from exc
        if current_head != expected_remote:
            raise CoordinationFreshnessError(
                f"member {repository_identity} physical HEAD is "
                f"{current_head!r}, expected {expected_remote!r}"
            )
        if branch != ws_member.branch:
            raise CoordinationFreshnessError(
                f"member {repository_identity} branch is {branch!r}, "
                f"expected {ws_member.branch!r}"
            )
        if status.strip():
            raise CoordinationFreshnessError(
                f"member {repository_identity} worktree is not clean including untracked"
            )

    @staticmethod
    def _derive_operation_id(
        workspace_id: str,
        repository_identity: str,
        recorded_head: str,
        target_remote_head: str,
    ) -> str:
        joined = (
            f"{workspace_id}\n{repository_identity}\n{recorded_head}\n"
            f"{target_remote_head}\n"
        )
        return "coord-freshness-ff-" + hashlib.sha256(
            joined.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _canonical_git_evidence(
        *, branch: str, local: str, recorded: str, remote: str
    ) -> str:
        return json.dumps(
            {
                "branch": branch,
                "local": local,
                "recorded": recorded,
                "remote": remote,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _parse_git_evidence(evidence: Optional[str]) -> Dict[str, str]:
        if evidence is None:
            raise CoordinationFreshnessError("git evidence is None")
        try:
            payload = json.loads(evidence)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CoordinationFreshnessError(
                f"git evidence is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "branch",
            "local",
            "recorded",
            "remote",
        }:
            raise CoordinationFreshnessError("git evidence keys mismatch")
        if json.dumps(payload, sort_keys=True, separators=(",", ":")) != evidence:
            raise CoordinationFreshnessError(
                "git evidence is not in canonical serialization"
            )
        for key in ("local", "recorded", "remote"):
            value = payload[key]
            if not isinstance(value, str) or len(value) not in (40, 64):
                raise CoordinationFreshnessError(
                    f"git evidence field {key!r} must be a 40- or 64-character string"
                )
            try:
                int(value, 16)
            except ValueError:
                raise CoordinationFreshnessError(
                    f"git evidence field {key!r} must be ASCII hex"
                ) from None
        if not isinstance(payload["branch"], str) or not payload["branch"].strip():
            raise CoordinationFreshnessError(
                "git evidence field 'branch' must be a nonblank string"
            )
        return payload

    def _owned_transaction(self, callback) -> Any:
        if self._conn.in_transaction:
            raise CoordinationFreshnessError("active transaction not allowed")
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
            if isinstance(exc, CoordinationFreshnessError):
                raise
            if isinstance(
                exc, (JournalRejected, sqlite3.Error, CoordinationWorkspaceError)
            ):
                raise CoordinationFreshnessError(str(exc)) from exc
            raise CoordinationFreshnessError(str(exc)) from exc

    def _journal_append_prepared(self, intent: JournalIntent) -> None:
        self._owned_transaction(lambda: self._journal.append_prepared(intent))

    def _journal_append_resume_prepared(
        self, intent: JournalIntent, at: int
    ) -> None:
        self._owned_transaction(
            lambda: self._journal.append_resume_prepared(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        )

    def _journal_append_ff_verified(
        self,
        intent: JournalIntent,
        ws_member: _WorkspaceMember,
        local: str,
        remote: str,
        at: int,
    ) -> None:
        self._owned_transaction(
            lambda: self._journal.append_verified(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=(
                    f"local={local};remote={remote};"
                    f"branch={ws_member.branch};ff=true"
                ),
                observed_filesystem_evidence=(
                    f"exists=true;target={ws_member.target_path}"
                ),
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        )

    def _journal_append_failed(
        self,
        intent: JournalIntent,
        ws_member: _WorkspaceMember,
        at: int,
        *,
        error_disposition: str,
        recovery_disposition: str,
    ) -> None:
        try:
            current_head = self._executor._git(
                ws_member.target_path, "rev-parse", "HEAD"
            )
        except _WorkspaceRejected:
            current_head = "unknown"
        self._owned_transaction(
            lambda: self._journal.append_failed(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=f"head={current_head}",
                observed_filesystem_evidence=(
                    f"exists={'true' if os.path.exists(ws_member.target_path) else 'false'};"
                    f"target={ws_member.target_path}"
                ),
                error_disposition=error_disposition,
                recovery_disposition=recovery_disposition,
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        )

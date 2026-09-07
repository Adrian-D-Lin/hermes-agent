"""Adrian Kanban workspace foundation."""

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .journal import ExternalOperationJournal, JournalIntent, JournalRejected

__all__ = ()


class _WorkspaceRejected(ValueError):
    pass


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")


def _validate_nonblank_str(value, name):
    if not isinstance(value, str):
        raise _WorkspaceRejected(f"{name} must be str")
    if not value.strip():
        raise _WorkspaceRejected(f"{name} must be nonblank")
    return value


def _validate_absolute_str(value, name):
    _validate_nonblank_str(value, name)
    if not os.path.isabs(value):
        raise _WorkspaceRejected(f"{name} must be absolute")
    return value


def _validate_sha(value, name):
    _validate_nonblank_str(value, name)
    if not _SHA_RE.match(value):
        raise _WorkspaceRejected(f"{name} must be a valid SHA")
    return value


def _validate_bool(value, name):
    if not isinstance(value, bool):
        raise _WorkspaceRejected(f"{name} must be bool")
    return value


@dataclass(frozen=True)
class _RepositoryRegistration:
    repository_identity: str
    repository_root: str
    controlled_worktree_root: str

    def __post_init__(self):
        _validate_nonblank_str(self.repository_identity, "repository_identity")
        _validate_absolute_str(self.repository_root, "repository_root")
        _validate_absolute_str(self.controlled_worktree_root, "controlled_worktree_root")


@dataclass(frozen=True)
class _WorkspaceMember:
    workspace_id: str
    repository_identity: str
    repository_root: str
    controlled_worktree_root: str
    relative_path: str
    target_path: str
    branch: str
    required_base_sha: str
    observed_head: Optional[str]
    member_state: str

    def __post_init__(self):
        _validate_nonblank_str(self.workspace_id, "workspace_id")
        _validate_nonblank_str(self.repository_identity, "repository_identity")
        _validate_absolute_str(self.repository_root, "repository_root")
        _validate_absolute_str(self.controlled_worktree_root, "controlled_worktree_root")
        _validate_nonblank_str(self.relative_path, "relative_path")
        _validate_absolute_str(self.target_path, "target_path")
        _validate_nonblank_str(self.branch, "branch")
        _validate_sha(self.required_base_sha, "required_base_sha")
        if self.observed_head is not None:
            _validate_sha(self.observed_head, "observed_head")
        _validate_nonblank_str(self.member_state, "member_state")


@dataclass(frozen=True)
class _WorkspacePlan:
    workspace_id: str
    initiative_id: str
    segment_id: str
    controller_binding_ref: str
    members: Tuple[_WorkspaceMember, ...]

    def __post_init__(self):
        _validate_nonblank_str(self.workspace_id, "workspace_id")
        _validate_nonblank_str(self.initiative_id, "initiative_id")
        _validate_nonblank_str(self.segment_id, "segment_id")
        _validate_nonblank_str(self.controller_binding_ref, "controller_binding_ref")
        if not isinstance(self.members, tuple):
            raise _WorkspaceRejected("members must be tuple")
        if len(self.members) < 1:
            raise _WorkspaceRejected("members must contain at least one member")
        for m in self.members:
            if not isinstance(m, _WorkspaceMember):
                raise _WorkspaceRejected("members must contain _WorkspaceMember")


@dataclass(frozen=True)
class _MemberVerification:
    repository_identity: str
    target_path: str
    observed_head: Optional[str]
    branch_matches: bool
    base_contained: bool
    common_repository: Optional[str]
    ready: bool
    failures: Tuple[str, ...]

    def __post_init__(self):
        _validate_nonblank_str(self.repository_identity, "repository_identity")
        _validate_absolute_str(self.target_path, "target_path")
        if self.observed_head is not None:
            _validate_sha(self.observed_head, "observed_head")
        _validate_bool(self.branch_matches, "branch_matches")
        _validate_bool(self.base_contained, "base_contained")
        if self.common_repository is not None:
            _validate_absolute_str(self.common_repository, "common_repository")
        _validate_bool(self.ready, "ready")
        if not isinstance(self.failures, tuple):
            raise _WorkspaceRejected("failures must be tuple")
        for f in self.failures:
            _validate_nonblank_str(f, "failure")


class _TrustedRepositoryRegistry:
    def __init__(self, registrations: Tuple[_RepositoryRegistration, ...] = ()):
        if not isinstance(registrations, tuple):
            raise _WorkspaceRejected("registrations must be tuple")
        seen = set()
        for r in registrations:
            if not isinstance(r, _RepositoryRegistration):
                raise _WorkspaceRejected("registrations must contain _RepositoryRegistration")
            if r.repository_identity in seen:
                raise _WorkspaceRejected(f"duplicate repository_identity in registry: {r.repository_identity}")
            seen.add(r.repository_identity)
        self._registrations = registrations

    def lookup(self, repository_identity: str) -> _RepositoryRegistration:
        _validate_nonblank_str(repository_identity, "repository_identity")
        for r in self._registrations:
            if r.repository_identity == repository_identity:
                return r
        raise _WorkspaceRejected(f"repository_identity not found in registry: {repository_identity}")


class _SegmentWorkspaceController:
    def __init__(self, conn, registry: _TrustedRepositoryRegistry):
        if not isinstance(conn, sqlite3.Connection):
            raise _WorkspaceRejected("conn must be sqlite3.Connection")
        if not isinstance(registry, _TrustedRepositoryRegistry):
            raise _WorkspaceRejected("registry must be _TrustedRepositoryRegistry")
        self._conn = conn
        self._registry = registry

    def load(self, *, workspace_id: str, expected_initiative_id: str, expected_segment_id: str, expected_controller_binding: str) -> _WorkspacePlan:
        _validate_nonblank_str(workspace_id, "workspace_id")
        _validate_nonblank_str(expected_initiative_id, "expected_initiative_id")
        _validate_nonblank_str(expected_segment_id, "expected_segment_id")
        _validate_nonblank_str(expected_controller_binding, "expected_controller_binding")

        cur = self._conn.execute(
            "SELECT initiative_id, segment_id, controller_binding_ref FROM segment_workspaces WHERE workspace_id=? AND active=1",
            (workspace_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise _WorkspaceRejected(f"workspace not found or inactive: {workspace_id}")
        initiative_id, segment_id, controller_binding_ref = row
        _validate_nonblank_str(initiative_id, "initiative_id")
        _validate_nonblank_str(segment_id, "segment_id")
        _validate_nonblank_str(controller_binding_ref, "controller_binding_ref")

        if initiative_id != expected_initiative_id:
            raise _WorkspaceRejected(f"initiative mismatch: expected {expected_initiative_id}, got {initiative_id}")
        if segment_id != expected_segment_id:
            raise _WorkspaceRejected(f"segment mismatch: expected {expected_segment_id}, got {segment_id}")
        if controller_binding_ref != expected_controller_binding:
            raise _WorkspaceRejected(f"controller mismatch: expected {expected_controller_binding}, got {controller_binding_ref}")

        cur = self._conn.execute(
            "SELECT repository_identity, relative_path, branch, required_base_sha, observed_head, member_state FROM segment_workspace_members WHERE workspace_id=? ORDER BY repository_identity",
            (workspace_id,),
        )
        rows = cur.fetchall()

        if not rows:
            raise _WorkspaceRejected("workspace must have at least one member")

        members = []
        for row in rows:
            repository_identity, relative_path, branch, required_base_sha, observed_head, member_state = row
            _validate_nonblank_str(repository_identity, "repository_identity")
            _validate_nonblank_str(relative_path, "relative_path")
            _validate_nonblank_str(branch, "branch")
            _validate_sha(required_base_sha, "required_base_sha")
            if observed_head is not None:
                _validate_sha(observed_head, "observed_head")
            _validate_nonblank_str(member_state, "member_state")

            reg = self._registry.lookup(repository_identity)
            controlled_root = reg.controlled_worktree_root

            if os.path.isabs(relative_path):
                raise _WorkspaceRejected(f"relative path must not be absolute: {relative_path}")
            parts = relative_path.replace("\\", "/").split("/")
            for part in parts:
                if part in ("", ".", ".."):
                    raise _WorkspaceRejected(f"relative path contains invalid component: {part}")

            resolved_target = os.path.normpath(os.path.join(controlled_root, relative_path))
            try:
                Path(resolved_target).resolve().relative_to(Path(controlled_root).resolve())
            except ValueError:
                raise _WorkspaceRejected(f"relative path resolves outside controlled root: {relative_path}")

            members.append(
                _WorkspaceMember(
                    workspace_id=workspace_id,
                    repository_identity=repository_identity,
                    repository_root=reg.repository_root,
                    controlled_worktree_root=controlled_root,
                    relative_path=relative_path,
                    target_path=resolved_target,
                    branch=branch,
                    required_base_sha=required_base_sha,
                    observed_head=observed_head,
                    member_state=member_state,
                )
            )

        return _WorkspacePlan(
            workspace_id=workspace_id,
            initiative_id=initiative_id,
            segment_id=segment_id,
            controller_binding_ref=controller_binding_ref,
            members=tuple(members),
        )


@dataclass(frozen=True)
class _MergeVerification:
    repository_identity: str
    target_path: str
    source_head: str
    merge_head: Optional[str]
    remote_main_head: str
    merge_commit: bool
    source_contained: bool
    remote_contained: bool
    ready: bool
    failures: Tuple[str, ...]

    def __post_init__(self):
        _validate_nonblank_str(self.repository_identity, "repository_identity")
        _validate_nonblank_str(self.target_path, "target_path")
        _validate_absolute_str(self.target_path, "target_path")
        _validate_sha(self.source_head, "source_head")
        if self.merge_head is not None:
            _validate_sha(self.merge_head, "merge_head")
        _validate_sha(self.remote_main_head, "remote_main_head")
        _validate_bool(self.merge_commit, "merge_commit")
        _validate_bool(self.source_contained, "source_contained")
        _validate_bool(self.remote_contained, "remote_contained")
        _validate_bool(self.ready, "ready")
        if not isinstance(self.failures, tuple):
            raise _WorkspaceRejected("failures must be a tuple")
        for f in self.failures:
            _validate_nonblank_str(f, "failure item")


class _GitWorkspaceExecutor:
    def __init__(self, timeout: float = 30):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise _WorkspaceRejected("timeout must be numeric")
        if timeout <= 0:
            raise _WorkspaceRejected("timeout must be positive")
        self._timeout = timeout

    def _run(self, argv: list, cwd: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                cwd=cwd,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
            )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
            raise _WorkspaceRejected(f"git command failed: {e}") from e

    def _git(self, cwd: str, *args: str, allowed_returncodes: Tuple[int, ...] = (0,)) -> str:
        argv = ["git"] + list(args)
        result = self._run(argv, cwd)
        if result.returncode not in allowed_returncodes:
            stderr = result.stderr.strip() if result.stderr else ""
            raise _WorkspaceRejected(f"git command failed: {stderr}")
        stdout = result.stdout.strip() if result.stdout else ""
        return stdout

    def _resolve_git_common_dir(self, cwd: str) -> Optional[str]:
        try:
            stdout = self._git(cwd, "rev-parse", "--git-common-dir", allowed_returncodes=(0,))
        except _WorkspaceRejected:
            return None
        if not stdout:
            return None
        if os.path.isabs(stdout):
            resolved = os.path.realpath(stdout)
        else:
            resolved = os.path.realpath(os.path.join(cwd, stdout))
        return resolved

    def verify(self, member: _WorkspaceMember) -> _MemberVerification:
        failures = []
        observed_head = None
        branch_matches = False
        base_contained = False
        common_repository = None

        target_path = member.target_path
        repository_root = member.repository_root

        if not os.path.exists(target_path):
            return _MemberVerification(
                repository_identity=member.repository_identity,
                target_path=target_path,
                observed_head=None,
                branch_matches=False,
                base_contained=False,
                common_repository=None,
                ready=False,
                failures=("member_absent",),
            )

        target_common = self._resolve_git_common_dir(target_path)
        if target_common is None:
            failures.append("not_git_worktree")
            return _MemberVerification(
                repository_identity=member.repository_identity,
                target_path=target_path,
                observed_head=None,
                branch_matches=False,
                base_contained=False,
                common_repository=None,
                ready=False,
                failures=tuple(failures),
            )

        repo_common = self._resolve_git_common_dir(repository_root)
        if repo_common is None:
            failures.append("not_git_worktree")
            return _MemberVerification(
                repository_identity=member.repository_identity,
                target_path=target_path,
                observed_head=None,
                branch_matches=False,
                base_contained=False,
                common_repository=None,
                ready=False,
                failures=tuple(failures),
            )

        if target_common != repo_common:
            failures.append("repository_mismatch")

        try:
            stdout = self._git(target_path, "symbolic-ref", "--short", "HEAD", allowed_returncodes=(0,))
            if stdout == member.branch:
                branch_matches = True
            else:
                failures.append("branch_mismatch")
        except _WorkspaceRejected:
            failures.append("branch_mismatch")

        try:
            stdout = self._git(target_path, "rev-parse", "HEAD", allowed_returncodes=(0,))
            observed_head = stdout
        except _WorkspaceRejected:
            failures.append("head_unavailable")

        try:
            result = self._run(
                ["git", "merge-base", "--is-ancestor", member.required_base_sha, "HEAD"],
                target_path,
            )
            if result.returncode == 0:
                base_contained = True
            elif result.returncode == 1:
                base_contained = False
            else:
                base_contained = False
        except _WorkspaceRejected:
            base_contained = False

        if not base_contained:
            failures.append("base_not_contained")

        common_repository = target_common

        ready = len(failures) == 0

        return _MemberVerification(
            repository_identity=member.repository_identity,
            target_path=target_path,
            observed_head=observed_head,
            branch_matches=branch_matches,
            base_contained=base_contained,
            common_repository=common_repository,
            ready=ready,
            failures=tuple(failures),
        )

    def materialize(self, member: _WorkspaceMember) -> _MemberVerification:
        target_path = member.target_path
        repository_root = member.repository_root
        branch = member.branch
        required_base_sha = member.required_base_sha

        stdout = self._git(repository_root, "rev-parse", "origin/main", allowed_returncodes=(0,))
        if stdout != required_base_sha:
            raise _WorkspaceRejected(f"origin/main does not match required base sha")

        self._git(repository_root, "check-ref-format", "--branch", branch, allowed_returncodes=(0,))

        if os.path.exists(target_path):
            verification = self.verify(member)
            if verification.ready:
                return verification
            raise _WorkspaceRejected(f"existing target not ready: {verification.failures}")

        target_parent = os.path.dirname(target_path)
        os.makedirs(target_parent, exist_ok=True)

        result = self._run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            repository_root,
        )
        if result.returncode == 0:
            branch_exists = True
        elif result.returncode == 1:
            branch_exists = False
        else:
            raise _WorkspaceRejected(f"show-ref failed with rc={result.returncode}")

        if branch_exists:
            argv = ["git", "worktree", "add", target_path, branch]
        else:
            argv = ["git", "worktree", "add", "-b", branch, target_path, required_base_sha]

        result = self._run(argv, repository_root)
        if result.returncode != 0:
            raise _WorkspaceRejected(f"worktree add failed: {result.stderr.strip()}")

        verification = self.verify(member)
        if not verification.ready:
            raise _WorkspaceRejected(f"materialized target not ready: {verification.failures}")
        return verification


    def _remote_main_head(self, repository_root):
        out = self._git(repository_root, 'ls-remote', '--heads', 'origin', 'refs/heads/main')
        lines = [l for l in out.splitlines() if l.strip()]
        if len(lines) != 1:
            raise _WorkspaceRejected("expected exactly one line from ls-remote")
        parts = lines[0].split()
        if len(parts) != 2:
            raise _WorkspaceRejected("malformed ls-remote output")
        sha, ref = parts
        if ref != 'refs/heads/main':
            raise _WorkspaceRejected("unexpected ref in ls-remote output")
        _validate_sha(sha, "ls-remote sha")
        return sha

    def _tracked_clean(self, cwd):
        out = self._git(cwd, 'status', '--porcelain', '--untracked-files=no')
        return out.strip() == ''

    def _is_ancestor(self, cwd, ancestor, descendant):
        proc = self._run(['git', 'merge-base', '--is-ancestor', ancestor, descendant], cwd)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise _WorkspaceRejected(f"git merge-base --is-ancestor failed with rc={proc.returncode}")

    def verify_merge(self, member, *, expected_main_sha, expected_source_head):
        if not isinstance(member, _WorkspaceMember):
            raise _WorkspaceRejected("invalid member type")
        _validate_sha(expected_main_sha, "expected_main_sha")
        _validate_sha(expected_source_head, "expected_source_head")

        member_verification = self.verify(member)

        failures = []
        if not member_verification.ready or member_verification.observed_head != expected_source_head:
            failures.append('source_mismatch')

        branch = self._git(member.repository_root, 'symbolic-ref', '--quiet', '--short', 'HEAD').strip()
        if branch != 'main':
            failures.append('main_branch_mismatch')

        if not self._tracked_clean(member.repository_root):
            failures.append('main_dirty')

        if failures:
            return _MergeVerification(
                repository_identity=member.repository_identity,
                target_path=member.target_path,
                source_head=expected_source_head,
                merge_head=None,
                remote_main_head=self._remote_main_head(member.repository_root),
                merge_commit=False,
                source_contained=False,
                remote_contained=False,
                ready=False,
                failures=tuple(failures),
            )

        local_head = self._git(member.repository_root, 'rev-parse', 'HEAD').strip()
        remote_main = self._remote_main_head(member.repository_root)

        if local_head == expected_main_sha:
            if remote_main == expected_main_sha:
                return _MergeVerification(
                    repository_identity=member.repository_identity,
                    target_path=member.target_path,
                    source_head=expected_source_head,
                    merge_head=None,
                    remote_main_head=remote_main,
                    merge_commit=False,
                    source_contained=False,
                    remote_contained=False,
                    ready=False,
                    failures=('merge_absent',),
                )
            else:
                return _MergeVerification(
                    repository_identity=member.repository_identity,
                    target_path=member.target_path,
                    source_head=expected_source_head,
                    merge_head=None,
                    remote_main_head=remote_main,
                    merge_commit=False,
                    source_contained=False,
                    remote_contained=False,
                    ready=False,
                    failures=('remote_diverged',),
                )

        try:
            parent1 = self._git(member.repository_root, 'rev-parse', 'HEAD^1').strip()
            parent2 = self._git(member.repository_root, 'rev-parse', 'HEAD^2').strip()
        except _WorkspaceRejected:
            return _MergeVerification(
                repository_identity=member.repository_identity,
                target_path=member.target_path,
                source_head=expected_source_head,
                merge_head=None,
                remote_main_head=remote_main,
                merge_commit=False,
                source_contained=False,
                remote_contained=False,
                ready=False,
                failures=('unsafe_main_state',),
            )

        qualifying = (parent1 == expected_main_sha and parent2 == expected_source_head and self._is_ancestor(member.repository_root, expected_source_head, local_head))

        if not qualifying:
            return _MergeVerification(
                repository_identity=member.repository_identity,
                target_path=member.target_path,
                source_head=expected_source_head,
                merge_head=None,
                remote_main_head=remote_main,
                merge_commit=False,
                source_contained=False,
                remote_contained=False,
                ready=False,
                failures=('unsafe_main_state',),
            )

        merge_commit = True
        source_contained = True
        merge_head = local_head

        if remote_main == local_head:
            return _MergeVerification(
                repository_identity=member.repository_identity,
                target_path=member.target_path,
                source_head=expected_source_head,
                merge_head=merge_head,
                remote_main_head=remote_main,
                merge_commit=merge_commit,
                source_contained=source_contained,
                remote_contained=True,
                ready=True,
                failures=(),
            )
        elif remote_main == expected_main_sha:
            return _MergeVerification(
                repository_identity=member.repository_identity,
                target_path=member.target_path,
                source_head=expected_source_head,
                merge_head=merge_head,
                remote_main_head=remote_main,
                merge_commit=merge_commit,
                source_contained=source_contained,
                remote_contained=False,
                ready=False,
                failures=('publish_pending',),
            )
        else:
            return _MergeVerification(
                repository_identity=member.repository_identity,
                target_path=member.target_path,
                source_head=expected_source_head,
                merge_head=merge_head,
                remote_main_head=remote_main,
                merge_commit=merge_commit,
                source_contained=source_contained,
                remote_contained=False,
                ready=False,
                failures=('remote_diverged',),
            )

    def merge_to_origin_main(self, member, *, expected_main_sha, expected_source_head):
        verification = self.verify_merge(member, expected_main_sha=expected_main_sha, expected_source_head=expected_source_head)

        if verification.ready:
            return verification

        if 'remote_diverged' in verification.failures:
            raise _WorkspaceRejected("origin/main has diverged")

        if 'publish_pending' in verification.failures:
            proc = self._run(['git', 'push', 'origin', f"{verification.merge_head}:refs/heads/main"], member.repository_root)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or '').strip()[:200]
                raise _WorkspaceRejected(f"push failed: {err}")
            final = self.verify_merge(member, expected_main_sha=expected_main_sha, expected_source_head=expected_source_head)
            if final.ready:
                return final
            raise _WorkspaceRejected("final verification failed after push")

        if 'merge_absent' in verification.failures:
            msg = f"Merge {member.workspace_id}/{member.repository_identity}"
            proc = self._run(['git', 'merge', '--no-ff', '--no-edit', '-m', msg, expected_source_head], member.repository_root)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or '').strip()[:200]
                raise _WorkspaceRejected(f"merge failed: {err}")

            reverified = self.verify_merge(member, expected_main_sha=expected_main_sha, expected_source_head=expected_source_head)
            if reverified.ready:
                return reverified
            if 'publish_pending' in reverified.failures:
                proc = self._run(['git', 'push', 'origin', f"{reverified.merge_head}:refs/heads/main"], member.repository_root)
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or '').strip()[:200]
                    raise _WorkspaceRejected(f"push failed: {err}")
                final = self.verify_merge(member, expected_main_sha=expected_main_sha, expected_source_head=expected_source_head)
                if final.ready:
                    return final
                raise _WorkspaceRejected("final verification failed after push")
            raise _WorkspaceRejected("unexpected state after merge")

        raise _WorkspaceRejected("cannot merge in current state")


class _JournaledWorkspaceOperations:
    def __init__(self, conn, journal, executor):
        if type(conn) is not sqlite3.Connection:
            raise _WorkspaceRejected("connection must be sqlite3.Connection")
        if type(journal) is not ExternalOperationJournal:
            raise _WorkspaceRejected("journal must be ExternalOperationJournal")
        if type(executor) is not _GitWorkspaceExecutor:
            raise _WorkspaceRejected("executor must be _GitWorkspaceExecutor")
        if journal._conn is not conn:
            raise _WorkspaceRejected("journal connection mismatch")
        self._conn = conn
        self._journal = journal
        self._executor = executor

    def _validate_intent(self, intent, member):
        if type(member) is not _WorkspaceMember:
            raise _WorkspaceRejected("member must be _WorkspaceMember")
        if type(intent) is not JournalIntent:
            raise _WorkspaceRejected("intent must be JournalIntent")
        if intent.operation_kind != "workspace_materialize":
            raise _WorkspaceRejected("intent operation_kind must be workspace_materialize")
        if intent.member_target != member.repository_identity:
            raise _WorkspaceRejected("intent member_target mismatch")
        if intent.workspace_id != member.workspace_id:
            raise _WorkspaceRejected("intent workspace_id mismatch")
        if intent.repository_identity != member.repository_identity:
            raise _WorkspaceRejected("intent repository_identity mismatch")
        expected_git = f"base={member.required_base_sha};branch={member.branch}"
        if intent.intended_git_evidence != expected_git:
            raise _WorkspaceRejected("intent intended_git_evidence mismatch")
        expected_fs = f"target={member.target_path}"
        if intent.intended_filesystem_evidence != expected_fs:
            raise _WorkspaceRejected("intent intended_filesystem_evidence mismatch")

    def _owned_transaction(self, callback):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            result = callback()
            self._conn.commit()
            return result
        except BaseException as e:
            try:
                self._conn.rollback()
            except Exception:
                pass
            if isinstance(e, _WorkspaceRejected):
                raise
            if isinstance(e, (JournalRejected, sqlite3.Error)):
                raise _WorkspaceRejected(str(e)) from e
            raise

    def _verified_evidence(self, member, v):
        return (
            f"head={v.observed_head};branch={member.branch};base_contained=true;common={v.common_repository}",
            f"exists=true;target={member.target_path}"
        )

    def _append_verified(self, intent, member, v, at):
        git_ev, fs_ev = self._verified_evidence(member, v)
        def _append():
            return self._journal.append_verified(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=git_ev,
                observed_filesystem_evidence=fs_ev,
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        return self._owned_transaction(_append)

    def _append_failed(self, intent, member, v, at, resumable):
        git_ev = f"head={v.observed_head}" if v.observed_head else None
        fs_ev = f"exists={'true' if os.path.exists(member.target_path) else 'false'};target={member.target_path}"
        error_disp = "effect absent after verification" if resumable else "unsafe workspace state after verification"
        recovery_disp = "resume" if resumable else "manual intervention required"
        def _append():
            return self._journal.append_failed(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=git_ev,
                observed_filesystem_evidence=fs_ev,
                actor_evidence=intent.actor_evidence,
                created_at=at,
                error_disposition=error_disp,
                recovery_disposition=recovery_disp,
            )
        return self._owned_transaction(_append)

    def _finish_observation(self, intent, member, v, at, after_materialize_attempt):
        if v.ready:
            self._append_verified(intent, member, v, at)
            return self._journal.verified_evidence(intent.operation_id, intent.member_target)
        elif v.failures == ('member_absent',):
            if after_materialize_attempt:
                self._append_failed(intent, member, v, at, resumable=True)
                return self._journal.head(intent.operation_id, intent.member_target)
            else:
                try:
                    self._executor.materialize(member)
                except _WorkspaceRejected:
                    pass
                v2 = self._executor.verify(member)
                return self._finish_observation(intent, member, v2, at, after_materialize_attempt=True)
        else:
            self._append_failed(intent, member, v, at, resumable=False)
            return self._journal.head(intent.operation_id, intent.member_target)

    def prepare_materialize(self, intent, member):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        self._validate_intent(intent, member)
        def _append():
            self._journal.append_prepared(intent)
            return self._journal.head(intent.operation_id, intent.member_target)
        return self._owned_transaction(_append)

    def run_materialize(self, intent, member, outcome_at):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        if type(outcome_at) is not int or outcome_at <= 0:
            raise _WorkspaceRejected("outcome_at must be positive exact int")
        self._validate_intent(intent, member)
        action = self._journal.recovery_action(intent.operation_id, intent.member_target)
        if action == "prepare":
            raise _WorkspaceRejected("must prepare first")
        if action == "consume_verified":
            return self._journal.verified_evidence(intent.operation_id, intent.member_target)
        if action == "halt":
            raise _WorkspaceRejected("halted")
        if action == "resume":
            def _append_resume():
                self._journal.append_resume_prepared(
                    operation_id=intent.operation_id,
                    member_target=intent.member_target,
                    actor_evidence=intent.actor_evidence,
                    created_at=outcome_at,
                )
            self._owned_transaction(_append_resume)
            action = "verify"
        if action == "verify":
            v = self._executor.verify(member)
            return self._finish_observation(intent, member, v, outcome_at, after_materialize_attempt=False)
        raise _WorkspaceRejected("unknown recovery action")

    def consume_materialization(self, intent, member, consumed_at):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        if type(consumed_at) is not int or consumed_at <= 0:
            raise _WorkspaceRejected("consumed_at must be positive exact int")
        self._validate_intent(intent, member)
        action = self._journal.recovery_action(intent.operation_id, intent.member_target)
        if action != "consume_verified":
            raise _WorkspaceRejected("verified")
        called_evidence = self._journal.verified_evidence(intent.operation_id, intent.member_target)
        v = self._executor.verify(member)
        if not v.ready:
            raise _WorkspaceRejected("verified")
        expected_git, expected_fs = self._verified_evidence(member, v)
        if called_evidence.observed_git_evidence != expected_git or called_evidence.observed_filesystem_evidence != expected_fs:
            raise _WorkspaceRejected("verified")
        def _consume():
            row = self._conn.execute(
                "SELECT observed_head, member_state, observed_at FROM segment_workspace_members WHERE workspace_id = ? AND repository_identity = ?",
                (member.workspace_id, member.repository_identity),
            ).fetchone()
            if row and row[1] == "materialized" and row[0] == v.observed_head and row[2] == consumed_at:
                return v
            if not row or row[1] != "planned":
                raise _WorkspaceRejected("consume")
            cur = self._conn.execute(
                "UPDATE segment_workspace_members SET observed_head = ?, member_state = 'materialized', observed_at = ? WHERE workspace_id = ? AND repository_identity = ? AND member_state = 'planned'",
                (v.observed_head, consumed_at, member.workspace_id, member.repository_identity),
            )
            if cur.rowcount != 1:
                raise _WorkspaceRejected("consume")
            return v
        return self._owned_transaction(_consume)
    def _validate_merge_intent(self, intent, member, expected_main_sha, expected_source_head):
        if type(member) is not _WorkspaceMember:
            raise _WorkspaceRejected("member must be _WorkspaceMember")
        if type(intent) is not JournalIntent:
            raise _WorkspaceRejected("intent must be JournalIntent")
        if intent.operation_kind != "workspace_merge":
            raise _WorkspaceRejected("intent operation_kind must be workspace_merge")
        if intent.member_target != member.repository_identity:
            raise _WorkspaceRejected("intent member_target mismatch")
        if intent.workspace_id != member.workspace_id:
            raise _WorkspaceRejected("intent workspace_id mismatch")
        if intent.repository_identity != member.repository_identity:
            raise _WorkspaceRejected("intent repository_identity mismatch")
        _validate_sha(expected_main_sha, "expected_main_sha")
        _validate_sha(expected_source_head, "expected_source_head")
        expected_git = (
            f"base={expected_main_sha};source={expected_source_head};"
            f"branch={member.branch}"
        )
        expected_fs = f"target={member.target_path};retain=true"
        if intent.intended_git_evidence != expected_git:
            raise _WorkspaceRejected("intent intended_git_evidence mismatch")
        if intent.intended_filesystem_evidence != expected_fs:
            raise _WorkspaceRejected("intent intended_filesystem_evidence mismatch")

    def prepare_merge(self, intent, member, expected_main_sha, expected_source_head):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        self._validate_merge_intent(intent, member, expected_main_sha, expected_source_head)
        def _append():
            self._journal.append_prepared(intent)
            return self._journal.head(intent.operation_id, intent.member_target)
        return self._owned_transaction(_append)

    def run_merge(self, intent, member, expected_main_sha, expected_source_head, *, outcome_at):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        if type(outcome_at) is not int or outcome_at <= 0:
            raise _WorkspaceRejected("outcome_at must be positive exact int")
        self._validate_merge_intent(intent, member, expected_main_sha, expected_source_head)
        action = self._journal.recovery_action(intent.operation_id, intent.member_target)
        if action == "prepare":
            raise _WorkspaceRejected("must prepare first")
        if action == "consume_verified":
            return self._journal.verified_evidence(intent.operation_id, intent.member_target)
        if action == "halt":
            raise _WorkspaceRejected("halted")
        if action == "resume":
            def _append_resume():
                self._journal.append_resume_prepared(
                    operation_id=intent.operation_id,
                    member_target=intent.member_target,
                    actor_evidence=intent.actor_evidence,
                    created_at=outcome_at,
                )
            self._owned_transaction(_append_resume)
            action = "verify"
        if action == "verify":
            v = self._executor.verify_merge(
                member,
                expected_main_sha=expected_main_sha,
                expected_source_head=expected_source_head,
            )
            if v.ready:
                self._append_merge_verified(intent, member, expected_main_sha, expected_source_head, v, outcome_at)
                return self._journal.verified_evidence(intent.operation_id, intent.member_target)
            if v.failures == ('merge_absent',) or v.failures == ('publish_pending',):
                try:
                    self._executor.merge_to_origin_main(
                        member,
                        expected_main_sha=expected_main_sha,
                        expected_source_head=expected_source_head,
                    )
                except _WorkspaceRejected:
                    reverified = self._executor.verify_merge(
                        member,
                        expected_main_sha=expected_main_sha,
                        expected_source_head=expected_source_head,
                    )
                    if reverified.ready:
                        self._append_merge_verified(intent, member, expected_main_sha, expected_source_head, reverified, outcome_at)
                        return self._journal.verified_evidence(intent.operation_id, intent.member_target)
                    if reverified.failures == ('merge_absent',) or reverified.failures == ('publish_pending',):
                        self._append_merge_failed(
                            intent, member, expected_main_sha, expected_source_head, reverified, outcome_at,
                            error_disposition='merge effect incomplete after verification',
                            recovery_disposition='resume'
                        )
                        return self._journal.head(intent.operation_id, intent.member_target)
                    self._append_merge_failed(
                        intent, member, expected_main_sha, expected_source_head, reverified, outcome_at,
                        error_disposition='unsafe merge state after verification',
                        recovery_disposition='manual intervention required'
                    )
                    return self._journal.head(intent.operation_id, intent.member_target)
                reverified = self._executor.verify_merge(
                    member,
                    expected_main_sha=expected_main_sha,
                    expected_source_head=expected_source_head,
                )
                if reverified.ready:
                    self._append_merge_verified(intent, member, expected_main_sha, expected_source_head, reverified, outcome_at)
                    return self._journal.verified_evidence(intent.operation_id, intent.member_target)
                if reverified.failures == ('merge_absent',) or reverified.failures == ('publish_pending',):
                    self._append_merge_failed(
                        intent, member, expected_main_sha, expected_source_head, reverified, outcome_at,
                        error_disposition='merge effect incomplete after verification',
                        recovery_disposition='resume'
                    )
                    return self._journal.head(intent.operation_id, intent.member_target)
                self._append_merge_failed(
                    intent, member, expected_main_sha, expected_source_head, reverified, outcome_at,
                    error_disposition='unsafe merge state after verification',
                    recovery_disposition='manual intervention required'
                )
                return self._journal.head(intent.operation_id, intent.member_target)
            self._append_merge_failed(
                intent, member, expected_main_sha, expected_source_head, v, outcome_at,
                error_disposition='unsafe merge state after verification',
                recovery_disposition='manual intervention required'
            )
            return self._journal.head(intent.operation_id, intent.member_target)
        raise _WorkspaceRejected("unknown recovery action")

    def consume_merge(self, intent, member, expected_main_sha, expected_source_head, *, consumed_at):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        if type(consumed_at) is not int or consumed_at <= 0:
            raise _WorkspaceRejected("consumed_at must be positive exact int")
        self._validate_merge_intent(intent, member, expected_main_sha, expected_source_head)
        action = self._journal.recovery_action(intent.operation_id, intent.member_target)
        if action != "consume_verified":
            raise _WorkspaceRejected("verified")
        called_evidence = self._journal.verified_evidence(intent.operation_id, intent.member_target)
        if called_evidence is None:
            raise _WorkspaceRejected("verified")
        v = self._executor.verify_merge(
            member,
            expected_main_sha=expected_main_sha,
            expected_source_head=expected_source_head,
        )
        if not v.ready:
            raise _WorkspaceRejected("verified")
        expected_git_ev, expected_fs_ev = self._merge_verified_evidence(member, expected_main_sha, expected_source_head, v)
        if (
            called_evidence.observed_git_evidence != expected_git_ev
            or called_evidence.observed_filesystem_evidence != expected_fs_ev
        ):
            raise _WorkspaceRejected("verified")
        def _consume():
            row = self._conn.execute(
                "SELECT observed_head, member_state, observed_at FROM segment_workspace_members WHERE workspace_id = ? AND repository_identity = ?",
                (member.workspace_id, member.repository_identity),
            ).fetchone()
            if row and row[1] == "merged" and row[0] == v.source_head and row[2] == consumed_at:
                return v
            if not row or row[1] != "materialized" or row[0] != expected_source_head:
                raise _WorkspaceRejected("consume")
            cur = self._conn.execute(
                "UPDATE segment_workspace_members SET observed_head = ?, member_state = 'merged', observed_at = ? WHERE workspace_id = ? AND repository_identity = ? AND member_state = 'materialized' AND observed_head = ?",
                (v.source_head, consumed_at, member.workspace_id, member.repository_identity, expected_source_head),
            )
            if cur.rowcount != 1:
                raise _WorkspaceRejected("consume")
            return v
        return self._owned_transaction(_consume)

    def _merge_verified_evidence(self, member, expected_main_sha, expected_source_head, v):
        return (
            f"base={expected_main_sha};source={expected_source_head};merge={v.merge_head};"
            f"remote={v.remote_main_head};merge_commit=true;source_contained=true;remote_contained=true",
            f"retained=true;target={member.target_path}",
        )

    def _append_merge_verified(self, intent, member, expected_main_sha, expected_source_head, v, at):
        git_ev, fs_ev = self._merge_verified_evidence(member, expected_main_sha, expected_source_head, v)
        def _append():
            return self._journal.append_verified(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=git_ev,
                observed_filesystem_evidence=fs_ev,
                actor_evidence=intent.actor_evidence,
                created_at=at,
            )
        return self._owned_transaction(_append)

    def _append_merge_failed(self, intent, member, expected_main_sha, expected_source_head, v, at, *, error_disposition, recovery_disposition):
        git_ev = (
            f"base={expected_main_sha};source={expected_source_head};"
            f"merge={v.merge_head if v.merge_head else 'none'};remote={v.remote_main_head}"
        )
        fs_ev = f"retained=true;target={member.target_path}"
        def _append():
            return self._journal.append_failed(
                operation_id=intent.operation_id,
                member_target=intent.member_target,
                observed_git_evidence=git_ev,
                observed_filesystem_evidence=fs_ev,
                actor_evidence=intent.actor_evidence,
                created_at=at,
                error_disposition=error_disposition,
                recovery_disposition=recovery_disposition,
            )
        return self._owned_transaction(_append)

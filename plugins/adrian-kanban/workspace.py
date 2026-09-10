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


def _validate_sha_or_none(value, name):
    if value is None:
        return None
    _validate_sha(value, name)
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
    required_base_sha: Optional[str]
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
        _validate_sha_or_none(self.required_base_sha, "required_base_sha")
        _validate_sha_or_none(self.observed_head, "observed_head")
        _validate_nonblank_str(self.member_state, "member_state")
        if self.member_state not in ("planned", "materialized", "merged", "retired"):
            raise _WorkspaceRejected("invalid member_state")
        if self.member_state == "planned":
            if self.observed_head is not None:
                raise _WorkspaceRejected("planned member must have NULL observed_head")
        else:
            if self.required_base_sha is None:
                raise _WorkspaceRejected(
                    f"{self.member_state} member requires non-null base"
                )
            if self.observed_head is None:
                raise _WorkspaceRejected(
                    f"{self.member_state} member requires non-null observed_head"
                )


def _common_segment_root(
    members: Tuple[_WorkspaceMember, ...],
    controlled_worktree_root: str,
    initiative_id: str,
    segment_id: str,
) -> str:
    """Return the one common parent directory above all repository-member
    worktrees.

    With the existing projection this is ``<shared root>/<escaped
    initiative>/<escaped segment>``.  Every member's stored relative path must
    project to exactly that location; a plan whose members do not share one
    segment root is rejected.
    """
    from .segment_manifest import _percent_encode

    expected = os.path.normpath(
        os.path.join(
            controlled_worktree_root,
            _percent_encode(initiative_id),
            _percent_encode(segment_id),
        )
    )
    for m in members:
        parts = m.relative_path.replace("\\", "/").split("/")
        if len(parts) < 2:
            raise _WorkspaceRejected(
                f"member {m.repository_identity} relative path does not project "
                "to a shared segment root"
            )
        projected = os.path.normpath(
            os.path.join(m.controlled_worktree_root, *parts[:-1])
        )
        if projected != expected:
            raise _WorkspaceRejected(
                f"members do not share one segment root: "
                f"{projected!r} differs from {expected!r}"
            )
    return expected


@dataclass(frozen=True)
class _WorkspacePlan:
    workspace_id: str
    initiative_id: str
    segment_id: str
    controller_binding_ref: str
    members: Tuple[_WorkspaceMember, ...]
    segment_root: str

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
        _validate_absolute_str(self.segment_root, "segment_root")


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
        shared_root = None
        for r in registrations:
            if not isinstance(r, _RepositoryRegistration):
                raise _WorkspaceRejected("registrations must contain _RepositoryRegistration")
            if r.repository_identity in seen:
                raise _WorkspaceRejected(f"duplicate repository_identity in registry: {r.repository_identity}")
            seen.add(r.repository_identity)
            normalized = os.path.normpath(r.controlled_worktree_root)
            if shared_root is None:
                shared_root = normalized
            elif normalized != shared_root:
                raise _WorkspaceRejected(
                    "all trusted repository registrations must share one "
                    f"identical controlled worktree root; got a different "
                    f"root than the shared root {shared_root!r}"
                )
        self._registrations = registrations
        self._controlled_worktree_root = shared_root

    @property
    def controlled_worktree_root(self) -> Optional[str]:
        """The one canonical controlled worktree root shared by every
        registered repository, or ``None`` when the registry is empty.

        Mixed roots are a configuration error: every trusted repository
        registration must use the identical shared root.
        """
        return self._controlled_worktree_root

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

    @staticmethod
    def _resolve_origin_main(repository_root: str) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "refs/remotes/origin/main"],
                cwd=repository_root,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _WorkspaceRejected(f"git command failed: {exc}") from exc
        if result.returncode != 0:
            raise _WorkspaceRejected(
                f"git rev-parse failed: {result.stderr.strip()}"
            )
        sha = result.stdout.strip()
        _validate_sha(sha, "origin/main")
        return sha

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
            _validate_sha_or_none(required_base_sha, "required_base_sha")
            _validate_sha_or_none(observed_head, "observed_head")
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

        members_tuple = tuple(members)
        segment_root = _common_segment_root(
            members_tuple,
            self._registry.controlled_worktree_root,
            initiative_id,
            segment_id,
        )

        return _WorkspacePlan(
            workspace_id=workspace_id,
            initiative_id=initiative_id,
            segment_id=segment_id,
            controller_binding_ref=controller_binding_ref,
            members=members_tuple,
            segment_root=segment_root,
        )

    def pin_planned_member_base(
        self,
        workspace_id: str,
        repository_identity: str,
        *,
        pinned_at: int,
    ) -> str:
        _validate_nonblank_str(workspace_id, "workspace_id")
        _validate_nonblank_str(repository_identity, "repository_identity")
        if type(pinned_at) is not int or pinned_at <= 0:
            raise _WorkspaceRejected("pinned_at must be positive exact int")

        registration = self._registry.lookup(repository_identity)
        trusted_sha = self._resolve_origin_main(registration.repository_root)

        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            workspace = self._conn.execute(
                "SELECT active FROM segment_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None or workspace[0] != 1:
                raise _WorkspaceRejected("workspace not found or inactive")
            row = self._conn.execute(
                "SELECT required_base_sha, observed_head, member_state "
                "FROM segment_workspace_members WHERE workspace_id = ? "
                "AND repository_identity = ?",
                (workspace_id, repository_identity),
            ).fetchone()
            if row is None:
                raise _WorkspaceRejected("member not found")
            base, head, state = row
            if state != "planned":
                raise _WorkspaceRejected("member not planned")
            if head is not None:
                raise _WorkspaceRejected("member observed_head not null")
            if base is not None:
                _validate_sha(base, "required_base_sha")
                if base == trusted_sha:
                    self._conn.commit()
                    return trusted_sha
                raise _WorkspaceRejected("base mismatch")
            cursor = self._conn.execute(
                "UPDATE segment_workspace_members SET required_base_sha = ?, "
                "observed_at = ? WHERE workspace_id = ? AND "
                "repository_identity = ? AND member_state = 'planned' AND "
                "required_base_sha IS NULL AND observed_head IS NULL",
                (trusted_sha, pinned_at, workspace_id, repository_identity),
            )
            if cursor.rowcount != 1:
                raise _WorkspaceRejected("update failed")
            self._conn.commit()
            return trusted_sha
        except BaseException as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            if isinstance(exc, _WorkspaceRejected):
                raise
            raise _WorkspaceRejected(str(exc)) from exc


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
        if member.required_base_sha is None:
            raise _WorkspaceRejected("required_base_sha is null")
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

        if required_base_sha is None:
            raise _WorkspaceRejected("required_base_sha is null")

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
        if member.required_base_sha is None:
            raise _WorkspaceRejected("required_base_sha is null")
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
        if member.required_base_sha is None:
            raise _WorkspaceRejected("required_base_sha is null")
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
    def retire_workspace(self, plan, *, retired_at):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        if type(plan) is not _WorkspacePlan:
            raise _WorkspaceRejected("plan must be _WorkspacePlan")
        if type(retired_at) is not int or retired_at <= 0:
            raise _WorkspaceRejected("retired_at must be positive exact int")

        ws = self._conn.execute(
            "SELECT controller_binding_ref, lifecycle_state, active, updated_at "
            "FROM segment_workspaces WHERE workspace_id = ?",
            (plan.workspace_id,),
        ).fetchone()
        if ws is None or ws[0] != plan.controller_binding_ref:
            raise _WorkspaceRejected("workspace binding mismatch")
        ws = tuple(ws)

        members = [
            tuple(row)
            for row in self._conn.execute(
                "SELECT repository_identity, observed_head, member_state, observed_at "
                "FROM segment_workspace_members WHERE workspace_id = ? "
                "ORDER BY repository_identity",
                (plan.workspace_id,),
            )
        ]
        member_identities = tuple(m[0] for m in members)
        if member_identities != tuple(m.repository_identity for m in plan.members):
            raise _WorkspaceRejected("member identities mismatch")

        # Map plan members by identity for verification
        plan_member_map = {m.repository_identity: m for m in plan.members}
        for plan_member in plan.members:
            if plan_member.required_base_sha is None:
                raise _WorkspaceRejected("required_base_sha is null")

        # Idempotent path
        if ws[1] == "retired" and ws[2] == 0:
            if ws[3] != retired_at:
                raise _WorkspaceRejected("retire mismatch")
            for m in members:
                if m[2] != "retired" or m[3] != retired_at:
                    raise _WorkspaceRejected("retire mismatch")

            for m in members:
                _validate_sha(m[1], "observed_head")
                plan_member = plan_member_map[m[0]]
                v = self._executor.verify_merge(
                    plan_member,
                    expected_main_sha=plan_member.required_base_sha,
                    expected_source_head=m[1],
                )
                if not v.ready:
                    raise _WorkspaceRejected("merge verification failed")
                status = self._executor._git(
                    plan_member.target_path, "status", "--porcelain"
                )
                if status != "":
                    raise _WorkspaceRejected("worktree must be clean")
            return member_identities

        # Normal path
        if ws[1] != "active" or ws[2] != 1:
            raise _WorkspaceRejected("workspace not active")

        for m in members:
            if m[2] != "merged" or m[1] is None:
                raise _WorkspaceRejected("all members must be merged")

        for m in members:
            _validate_sha(m[1], "observed_head")
            plan_member = plan_member_map[m[0]]
            v = self._executor.verify_merge(
                plan_member,
                expected_main_sha=plan_member.required_base_sha,
                expected_source_head=m[1],
            )
            if not v.ready:
                raise _WorkspaceRejected("merge verification failed")
            status = self._executor._git(
                plan_member.target_path, "status", "--porcelain"
            )
            if status != "":
                raise _WorkspaceRejected("worktree must be clean")

        # Capture preflight snapshots for re-verification in transaction
        preflight_ws = ws
        preflight_members = list(members)

        def _retire():
            # Re-read workspace
            ws_new = self._conn.execute(
                "SELECT controller_binding_ref, lifecycle_state, active, updated_at "
                "FROM segment_workspaces WHERE workspace_id = ?",
                (plan.workspace_id,),
            ).fetchone()
            if ws_new is None or tuple(ws_new) != preflight_ws:
                raise _WorkspaceRejected("workspace state changed")

            # Re-read members
            members_new = [
                tuple(row)
                for row in self._conn.execute(
                    "SELECT repository_identity, observed_head, member_state, observed_at "
                    "FROM segment_workspace_members WHERE workspace_id = ? "
                    "ORDER BY repository_identity",
                    (plan.workspace_id,),
                )
            ]
            if members_new != preflight_members:
                raise _WorkspaceRejected("member state changed")

            # Update members
            for m in members_new:
                cur = self._conn.execute(
                    "UPDATE segment_workspace_members SET member_state = 'retired', observed_at = ? "
                    "WHERE workspace_id = ? AND repository_identity = ? AND member_state = 'merged' AND observed_head = ?",
                    (retired_at, plan.workspace_id, m[0], m[1]),
                )
                if cur.rowcount != 1:
                    raise _WorkspaceRejected("member update failed")

            # Update workspace
            cur = self._conn.execute(
                "UPDATE segment_workspaces SET lifecycle_state = 'retired', active = 0, updated_at = ? "
                "WHERE workspace_id = ? AND controller_binding_ref = ? AND lifecycle_state = 'active' AND active = 1",
                (retired_at, plan.workspace_id, plan.controller_binding_ref),
            )
            if cur.rowcount != 1:
                raise _WorkspaceRejected("workspace update failed")

            return member_identities

        return self._owned_transaction(_retire)
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
    def activate_workspace(self, plan, *, activated_at):
        if self._conn.in_transaction:
            raise _WorkspaceRejected("active transaction not allowed")
        if type(plan) is not _WorkspacePlan:
            raise _WorkspaceRejected("plan must be _WorkspacePlan")
        if type(activated_at) is not int or activated_at <= 0:
            raise _WorkspaceRejected("activated_at must be positive exact int")

        ws = tuple(self._conn.execute(
            "SELECT controller_binding_ref, lifecycle_state, active, updated_at "
            "FROM segment_workspaces WHERE workspace_id = ?",
            (plan.workspace_id,),
        ).fetchone())
        if ws[0] != plan.controller_binding_ref:
            raise _WorkspaceRejected("workspace binding mismatch")

        members = [
            tuple(row)
            for row in self._conn.execute(
                "SELECT repository_identity, observed_head, member_state, observed_at "
                "FROM segment_workspace_members WHERE workspace_id = ? "
                "ORDER BY repository_identity",
                (plan.workspace_id,),
            )
        ]
        member_identities = tuple(m[0] for m in members)
        if member_identities != tuple(m.repository_identity for m in plan.members):
            raise _WorkspaceRejected("member identities mismatch")

        plan_member_map = {m.repository_identity: m for m in plan.members}
        for plan_member in plan.members:
            if plan_member.required_base_sha is None:
                raise _WorkspaceRejected("required_base_sha is null")

        # Idempotent path: already active. Reverify all members; keep original
        # updated_at.
        if ws[1] == "active" and ws[2] == 1:
            for m in members:
                if m[2] != "materialized" or m[1] is None:
                    raise _WorkspaceRejected("all members must be materialized")
                _validate_sha(m[1], "observed_head")
                plan_member = plan_member_map[m[0]]
                v = self._executor.verify(plan_member)
                if not v.ready:
                    raise _WorkspaceRejected("verification failed")
                if v.observed_head != m[1]:
                    raise _WorkspaceRejected("head mismatch")
            return member_identities

        # Normal path: planned -> active.
        if ws[1] != "planned" or ws[2] != 1:
            raise _WorkspaceRejected("workspace not planned")

        for m in members:
            if m[2] != "materialized" or m[1] is None:
                raise _WorkspaceRejected("all members must be materialized")
            _validate_sha(m[1], "observed_head")
            plan_member = plan_member_map[m[0]]
            v = self._executor.verify(plan_member)
            if not v.ready:
                raise _WorkspaceRejected("verification failed")
            if v.observed_head != m[1]:
                raise _WorkspaceRejected("head mismatch")

        preflight_ws = ws
        preflight_members = list(members)

        def _activate():
            ws_new = tuple(self._conn.execute(
                "SELECT controller_binding_ref, lifecycle_state, active, updated_at "
                "FROM segment_workspaces WHERE workspace_id = ?",
                (plan.workspace_id,),
            ).fetchone())
            if ws_new != preflight_ws:
                raise _WorkspaceRejected("workspace state changed")

            members_new = [
                tuple(row)
                for row in self._conn.execute(
                    "SELECT repository_identity, observed_head, member_state, observed_at "
                    "FROM segment_workspace_members WHERE workspace_id = ? "
                    "ORDER BY repository_identity",
                    (plan.workspace_id,),
                )
            ]
            if members_new != preflight_members:
                raise _WorkspaceRejected("member state changed")

            cur = self._conn.execute(
                "UPDATE segment_workspaces SET lifecycle_state = 'active', updated_at = ? "
                "WHERE workspace_id = ? AND controller_binding_ref = ? "
                "AND lifecycle_state = 'planned' AND active = 1",
                (activated_at, plan.workspace_id, plan.controller_binding_ref),
            )
            if cur.rowcount != 1:
                raise _WorkspaceRejected("workspace update failed")

            return member_identities

        return self._owned_transaction(_activate)

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


def _materialize_segment_workspace(conn, registry, *, initiative_id, segment_id, materialized_at) -> str:
    if conn.in_transaction:
        raise _WorkspaceRejected("active transaction not allowed")
    if type(registry) is not _TrustedRepositoryRegistry:
        raise _WorkspaceRejected("registry must be trusted")
    if not isinstance(initiative_id, str) or not initiative_id.strip():
        raise _WorkspaceRejected("initiative_id must be nonblank")
    if not isinstance(segment_id, str) or not segment_id.strip():
        raise _WorkspaceRejected("segment_id must be nonblank")
    if type(materialized_at) is not int or materialized_at <= 0:
        raise _WorkspaceRejected("materialized_at must be positive exact int")

    rows = tuple(
        conn.execute(
            "SELECT workspace_id FROM segment_workspaces "
            "WHERE initiative_id = ? AND segment_id = ? AND active = 1",
            (initiative_id, segment_id),
        ).fetchall()
    )
    if len(rows) != 1:
        raise _WorkspaceRejected("workspace state missing or ambiguous")
    workspace_id = rows[0][0]

    controller = _SegmentWorkspaceController(conn, registry)
    journal = ExternalOperationJournal(conn)
    operations = _JournaledWorkspaceOperations(conn, journal, _GitWorkspaceExecutor())

    binding = conn.execute(
        "SELECT controller_binding_ref FROM segment_workspaces WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()[0]

    plan = controller.load(
        workspace_id=workspace_id,
        expected_initiative_id=initiative_id,
        expected_segment_id=segment_id,
        expected_controller_binding=binding,
    )

    for member in sorted(plan.members, key=lambda m: m.repository_identity):
        if member.member_state == "materialized":
            continue
        if member.member_state != "planned":
            raise _WorkspaceRejected("member state unexpected")
        controller.pin_planned_member_base(workspace_id, member.repository_identity, pinned_at=materialized_at)
        plan = controller.load(
            workspace_id=workspace_id,
            expected_initiative_id=initiative_id,
            expected_segment_id=segment_id,
            expected_controller_binding=binding,
        )
        member = next(m for m in plan.members if m.repository_identity == member.repository_identity)
        op_id = f"workspace-materialize-{workspace_id}-{member.repository_identity}"
        intent = JournalIntent(
            operation_kind="workspace_materialize",
            operation_id=op_id,
            idempotency_id=op_id,
            member_target=member.repository_identity,
            workspace_id=workspace_id,
            repository_identity=member.repository_identity,
            actor_evidence="system:workspace-controller",
            intended_git_evidence=(
                f"base={member.required_base_sha};branch={member.branch}"
            ),
            intended_filesystem_evidence=f"target={member.target_path}",
            created_at=materialized_at,
        )
        try:
            action = journal.recovery_action(intent.operation_id, intent.member_target)
            if action == "prepare":
                operations.prepare_materialize(intent, member)
            operations.run_materialize(intent, member, outcome_at=materialized_at)
            operations.consume_materialization(intent, member, consumed_at=materialized_at)
        except Exception:
            raise _WorkspaceRejected(f"materialization failed for {member.repository_identity}")
        plan = controller.load(
            workspace_id=workspace_id,
            expected_initiative_id=initiative_id,
            expected_segment_id=segment_id,
            expected_controller_binding=plan.controller_binding_ref,
        )

    operations.activate_workspace(plan, activated_at=materialized_at)
    return plan.segment_root


def _validate_40hex(value, name):
    if not isinstance(value, str) or len(value) != 40:
        raise _WorkspaceRejected(f"{name} must be 40-char hex")
    for c in value:
        if c not in "0123456789abcdef":
            raise _WorkspaceRejected(f"{name} must be lowercase hex")
    return value


def _load_active_segment_workspace(conn, registry, *, initiative_id, segment_id):
    if conn.in_transaction:
        raise _WorkspaceRejected("active transaction not allowed")
    if type(registry) is not _TrustedRepositoryRegistry:
        raise _WorkspaceRejected("registry must be trusted")
    if not isinstance(initiative_id, str) or not initiative_id.strip():
        raise _WorkspaceRejected("initiative_id must be nonblank")
    if not isinstance(segment_id, str) or not segment_id.strip():
        raise _WorkspaceRejected("segment_id must be nonblank")
    rows = tuple(
        conn.execute(
            "SELECT workspace_id FROM segment_workspaces "
            "WHERE initiative_id = ? AND segment_id = ? AND active = 1",
            (initiative_id, segment_id),
        ).fetchall()
    )
    if len(rows) != 1:
        raise _WorkspaceRejected("workspace state missing or ambiguous")
    workspace_id = rows[0][0]
    binding = conn.execute(
        "SELECT controller_binding_ref FROM segment_workspaces WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()[0]
    controller = _SegmentWorkspaceController(conn, registry)
    plan = controller.load(
        workspace_id=workspace_id,
        expected_initiative_id=initiative_id,
        expected_segment_id=segment_id,
        expected_controller_binding=binding,
    )
    return workspace_id, controller, plan


def _load_segment_workspace(conn, registry, *, initiative_id, segment_id):
    """Load the exact workspace by initiative/segment without requiring active=1.

    Used by retirement where the workspace may already be retired (idempotent
    retry).  Rejects missing or ambiguous workspaces.
    """
    if conn.in_transaction:
        raise _WorkspaceRejected("active transaction not allowed")
    if type(registry) is not _TrustedRepositoryRegistry:
        raise _WorkspaceRejected("registry must be trusted")
    if not isinstance(initiative_id, str) or not initiative_id.strip():
        raise _WorkspaceRejected("initiative_id must be nonblank")
    if not isinstance(segment_id, str) or not segment_id.strip():
        raise _WorkspaceRejected("segment_id must be nonblank")
    rows = tuple(
        conn.execute(
            "SELECT workspace_id, initiative_id, segment_id, controller_binding_ref "
            "FROM segment_workspaces "
            "WHERE initiative_id = ? AND segment_id = ?",
            (initiative_id, segment_id),
        ).fetchall()
    )
    if len(rows) != 1:
        raise _WorkspaceRejected("workspace state missing or ambiguous")
    workspace_id, stored_initiative, stored_segment, binding = rows[0]
    if stored_initiative != initiative_id or stored_segment != segment_id:
        raise _WorkspaceRejected("workspace identity mismatch")
    # Build the plan manually since controller.load requires active=1.
    cur = conn.execute(
        "SELECT repository_identity, relative_path, branch, required_base_sha, "
        "observed_head, member_state FROM segment_workspace_members "
        "WHERE workspace_id=? ORDER BY repository_identity",
        (workspace_id,),
    )
    mrows = cur.fetchall()
    if not mrows:
        raise _WorkspaceRejected("workspace must have at least one member")
    members = []
    for mrow in mrows:
        rid, rel_path, branch, base_sha, obs_head, mstate = mrow
        reg = registry.lookup(rid)
        controlled_root = reg.controlled_worktree_root
        if os.path.isabs(rel_path):
            raise _WorkspaceRejected(f"relative path must not be absolute: {rel_path}")
        parts = rel_path.replace("\\", "/").split("/")
        for part in parts:
            if part in ("", ".", ".."):
                raise _WorkspaceRejected(f"relative path contains invalid component: {part}")
        resolved_target = os.path.normpath(os.path.join(controlled_root, rel_path))
        try:
            Path(resolved_target).resolve().relative_to(Path(controlled_root).resolve())
        except ValueError:
            raise _WorkspaceRejected(f"relative path resolves outside controlled root: {rel_path}")
        members.append(
            _WorkspaceMember(
                workspace_id=workspace_id,
                repository_identity=rid,
                repository_root=reg.repository_root,
                controlled_worktree_root=controlled_root,
                relative_path=rel_path,
                target_path=resolved_target,
                branch=branch,
                required_base_sha=base_sha,
                observed_head=obs_head,
                member_state=mstate,
            )
        )
    members_tuple = tuple(members)
    segment_root = _common_segment_root(
        members_tuple,
        registry.controlled_worktree_root,
        initiative_id,
        segment_id,
    )
    plan = _WorkspacePlan(
        workspace_id=workspace_id,
        initiative_id=initiative_id,
        segment_id=segment_id,
        controller_binding_ref=binding,
        members=members_tuple,
        segment_root=segment_root,
    )
    return workspace_id, None, plan


def _parse_merge_commit_sha(observed_git_evidence):
    if observed_git_evidence is None:
        raise _WorkspaceRejected("no verified journal evidence")
    merge_sha = None
    remote_sha = None
    for part in observed_git_evidence.split(";"):
        if part.startswith("merge="):
            merge_sha = part[len("merge="):]
        elif part.startswith("remote="):
            remote_sha = part[len("remote="):]
    if merge_sha is None:
        raise _WorkspaceRejected("no merge commit in journal evidence")
    _validate_40hex(merge_sha, "merge_commit_sha")
    if remote_sha is not None:
        _validate_40hex(remote_sha, "remote_main_head")
        if remote_sha != merge_sha:
            raise _WorkspaceRejected("remote containment failed")
    return merge_sha


def _merge_segment_workspace(conn, registry, *, initiative_id, segment_id, accepted_member_heads, merged_at) -> dict:
    if type(merged_at) is not int or merged_at <= 0:
        raise _WorkspaceRejected("merged_at must be positive exact int")
    if not isinstance(accepted_member_heads, tuple):
        raise _WorkspaceRejected("accepted_member_heads must be tuple")

    workspace_id, controller, plan = _load_active_segment_workspace(
        conn, registry, initiative_id=initiative_id, segment_id=segment_id
    )

    # Validate and sort accepted_member_heads by repository_identity
    entries = []
    seen_ids = set()
    for entry in accepted_member_heads:
        if not isinstance(entry, dict):
            raise _WorkspaceRejected("accepted_member_heads entries must be dicts")
        if set(entry.keys()) != {"repository_identity", "accepted_sha"}:
            raise _WorkspaceRejected("accepted_member_heads entry keys mismatch")
        rid = entry["repository_identity"]
        sha = entry["accepted_sha"]
        if not isinstance(rid, str) or not rid.strip():
            raise _WorkspaceRejected("repository_identity must be nonblank")
        _validate_40hex(sha, "accepted_sha")
        if rid in seen_ids:
            raise _WorkspaceRejected(f"duplicate repository_identity: {rid}")
        seen_ids.add(rid)
        entries.append((rid, sha))
    entries.sort(key=lambda x: x[0])

    member_by_id = {m.repository_identity: m for m in plan.members}
    if set(member_by_id.keys()) != seen_ids:
        raise _WorkspaceRejected("accepted_member_heads does not cover all members exactly once")

    accepted_map = dict(entries)
    executor = _GitWorkspaceExecutor()
    journal = ExternalOperationJournal(conn)
    operations = _JournaledWorkspaceOperations(conn, journal, executor)

    # Preflight every not-yet-merged member before any DB or journal mutation.
    # Already-merged members are reverified but their delivery SHA is read from
    # the stored observed_head (the sealed head).
    delivery_shas = {}
    for member in sorted(plan.members, key=lambda m: m.repository_identity):
        if member.member_state == "merged":
            # Reverify an already-merged member; use its stored observed_head.
            _validate_40hex(member.observed_head, "observed_head")
            delivery_shas[member.repository_identity] = member.observed_head
            continue
        if member.member_state != "materialized":
            raise _WorkspaceRejected(f"member {member.repository_identity} not materialized")
        v = executor.verify(member)
        if not v.ready:
            raise _WorkspaceRejected(
                f"preflight verification failed for {member.repository_identity}: {v.failures}"
            )
        status = executor._git(member.target_path, "status", "--porcelain")
        if status != "":
            raise _WorkspaceRejected(
                f"worktree not clean for {member.repository_identity}"
            )
        delivery_sha = v.observed_head
        _validate_40hex(delivery_sha, "delivery_sha")
        accepted_sha = accepted_map[member.repository_identity]
        if not executor._is_ancestor(member.target_path, accepted_sha, delivery_sha):
            raise _WorkspaceRejected(
                f"accepted SHA not ancestor of delivery for {member.repository_identity}"
            )
        delivery_shas[member.repository_identity] = delivery_sha

    # Head-seal: update every materialized member's observed_head to its verified
    # delivery SHA in one owned BEGIN IMMEDIATE transaction.
    def _seal_heads():
        for member in sorted(plan.members, key=lambda m: m.repository_identity):
            if member.member_state == "merged":
                continue
            cur = conn.execute(
                "UPDATE segment_workspace_members SET observed_head = ?, observed_at = ? "
                "WHERE workspace_id = ? AND repository_identity = ? "
                "AND member_state = 'materialized' AND observed_head = ?",
                (
                    delivery_shas[member.repository_identity],
                    merged_at,
                    workspace_id,
                    member.repository_identity,
                    member.observed_head,
                ),
            )
            if cur.rowcount != 1:
                raise _WorkspaceRejected("head seal update failed")
    operations._owned_transaction(_seal_heads)

    # Reload plan after head-seal
    plan = controller.load(
        workspace_id=workspace_id,
        expected_initiative_id=initiative_id,
        expected_segment_id=segment_id,
        expected_controller_binding=plan.controller_binding_ref,
    )
    member_by_id = {m.repository_identity: m for m in plan.members}

    # Merge each member in repository order
    member_merges = []
    for member in sorted(plan.members, key=lambda m: m.repository_identity):
        op_id = f"workspace-merge-{workspace_id}-{member.repository_identity}-{delivery_shas[member.repository_identity]}"
        intent = JournalIntent(
            operation_kind="workspace_merge",
            operation_id=op_id,
            idempotency_id=op_id,
            member_target=member.repository_identity,
            workspace_id=workspace_id,
            repository_identity=member.repository_identity,
            actor_evidence="system:workspace-controller",
            intended_git_evidence=(
                f"base={member.required_base_sha};source={delivery_shas[member.repository_identity]};branch={member.branch}"
            ),
            intended_filesystem_evidence=f"target={member.target_path};retain=true",
            created_at=merged_at,
        )
        try:
            action = journal.recovery_action(intent.operation_id, intent.member_target)
            if action == "prepare":
                operations.prepare_merge(
                    intent, member,
                    member.required_base_sha,
                    delivery_shas[member.repository_identity],
                )
            operations.run_merge(
                intent, member,
                member.required_base_sha,
                delivery_shas[member.repository_identity],
                outcome_at=merged_at,
            )
            operations.consume_merge(
                intent, member,
                member.required_base_sha,
                delivery_shas[member.repository_identity],
                consumed_at=merged_at,
            )
        except Exception:
            raise _WorkspaceRejected(
                f"merge failed for {member.repository_identity}"
            )
        evidence = journal.verified_evidence(intent.operation_id, intent.member_target)
        if evidence is None:
            raise _WorkspaceRejected(
                f"no verified journal evidence for {member.repository_identity}"
            )
        merge_commit_sha = _parse_merge_commit_sha(evidence.observed_git_evidence)
        member_merges.append({
            "repository_identity": member.repository_identity,
            "dev3_accepted_sha": accepted_map[member.repository_identity],
            "segment_delivery_sha": delivery_shas[member.repository_identity],
            "merge_operation_id": op_id,
            "merge_commit_sha": merge_commit_sha,
        })

    return {
        "workspace_id": workspace_id,
        "member_merges": member_merges,
    }


def _retire_segment_workspace(conn, registry, *, initiative_id, segment_id, retired_at) -> dict:
    if type(retired_at) is not int or retired_at <= 0:
        raise _WorkspaceRejected("retired_at must be positive exact int")

    workspace_id, controller, plan = _load_segment_workspace(
        conn, registry, initiative_id=initiative_id, segment_id=segment_id
    )

    journal = ExternalOperationJournal(conn)
    operations = _JournaledWorkspaceOperations(conn, journal, _GitWorkspaceExecutor())
    member_ids = operations.retire_workspace(plan, retired_at=retired_at)

    return {
        "workspace_id": workspace_id,
        "retired_member_ids": sorted(member_ids),
        "retired_at": retired_at,
    }

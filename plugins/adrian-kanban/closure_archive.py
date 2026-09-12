"""Permanent initiative archive and coordination-worktree retirement."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from .coordination_workspace import (
    CoordinationWorkspaceError,
    CoordinationWorkspaceStore,
)
from .workspace import (
    _RepositoryRegistration,
    _TrustedRepositoryRegistry,
    _WorkspaceMember,
    _WorkspaceRejected,
)

__all__ = [
    "ClosureArchiveError",
    "ClosureOperationJournal",
    "InitiativeArchiveController",
    "finalize_initiative_archive",
]


class ClosureArchiveError(Exception):
    """Raised when closure archival cannot be proved safe and complete."""


class ClosureOperationJournal:
    """Append-only recovery journal for the multi-effect closure sequence."""

    SUCCESS_STATES = (
        "prepared",
        "merged",
        "closed",
        "archive_verified",
        "retired",
        "consumed",
    )
    _SELECT = (
        "SELECT event_id, operation_id, initiative_id, workspace_id, ordinal, "
        "state, evidence_json, error_disposition, recovery_stage, "
        "actor_evidence, created_at FROM initiative_closure_operation_journal"
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        if type(conn) is not sqlite3.Connection:
            raise ClosureArchiveError("conn must be a sqlite3.Connection")
        self._conn = conn

    @staticmethod
    def _validate_identity(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ClosureArchiveError(f"{name} must be a nonblank string")
        return value.strip()

    @staticmethod
    def _validate_at(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ClosureArchiveError("at must be a positive integer")
        return value

    @staticmethod
    def _row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "event_id": row[0],
            "operation_id": row[1],
            "initiative_id": row[2],
            "workspace_id": row[3],
            "ordinal": row[4],
            "state": row[5],
            "evidence": json.loads(row[6]),
            "error_disposition": row[7],
            "recovery_stage": row[8],
            "actor_evidence": row[9],
            "created_at": row[10],
        }

    def history(self, operation_id: str) -> list[dict[str, Any]]:
        operation_id = self._validate_identity(operation_id, "operation_id")
        rows = self._conn.execute(
            self._SELECT + " WHERE operation_id = ? ORDER BY ordinal",
            (operation_id,),
        ).fetchall()
        return [self._row(row) for row in rows]

    def head(self, operation_id: str) -> Optional[dict[str, Any]]:
        history = self.history(operation_id)
        return history[-1] if history else None

    def next_stage(self, operation_id: str) -> Optional[str]:
        head = self.head(operation_id)
        if head is None:
            return "prepared"
        if head["state"] == "failed":
            return head["recovery_stage"]
        index = self.SUCCESS_STATES.index(head["state"])
        if index == len(self.SUCCESS_STATES) - 1:
            return None
        return self.SUCCESS_STATES[index + 1]

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise ClosureArchiveError("journal append requires an active transaction")

    def append_state(
        self,
        *,
        operation_id: str,
        initiative_id: str,
        workspace_id: Optional[str],
        state: str,
        evidence: dict[str, Any],
        actor_evidence: str,
        at: int,
    ) -> dict[str, Any]:
        self._require_transaction()
        operation_id = self._validate_identity(operation_id, "operation_id")
        initiative_id = self._validate_identity(initiative_id, "initiative_id")
        actor_evidence = self._validate_identity(actor_evidence, "actor_evidence")
        if workspace_id is not None:
            workspace_id = self._validate_identity(workspace_id, "workspace_id")
        at = self._validate_at(at)
        if not isinstance(evidence, dict):
            raise ClosureArchiveError("evidence must be a dict")
        expected = self.next_stage(operation_id)
        if state != expected:
            raise ClosureArchiveError(
                f"closure journal expected {expected!r}, got {state!r}"
            )
        ordinal = len(self.history(operation_id)) + 1
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        cursor = self._conn.execute(
            "INSERT INTO initiative_closure_operation_journal ("
            "operation_id, initiative_id, workspace_id, ordinal, state, "
            "evidence_json, error_disposition, recovery_stage, actor_evidence, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                operation_id,
                initiative_id,
                workspace_id,
                ordinal,
                state,
                canonical,
                actor_evidence,
                at,
            ),
        )
        row = self._conn.execute(
            self._SELECT + " WHERE event_id = ?", (cursor.lastrowid,)
        ).fetchone()
        if row is None:
            raise ClosureArchiveError("inserted closure event could not be read")
        return self._row(row)

    def append_failed(
        self,
        *,
        operation_id: str,
        initiative_id: str,
        workspace_id: Optional[str],
        evidence: dict[str, Any],
        error_disposition: str,
        recovery_stage: str,
        actor_evidence: str,
        at: int,
    ) -> dict[str, Any]:
        self._require_transaction()
        operation_id = self._validate_identity(operation_id, "operation_id")
        initiative_id = self._validate_identity(initiative_id, "initiative_id")
        error_disposition = self._validate_identity(
            error_disposition, "error_disposition"
        )
        actor_evidence = self._validate_identity(actor_evidence, "actor_evidence")
        if workspace_id is not None:
            workspace_id = self._validate_identity(workspace_id, "workspace_id")
        at = self._validate_at(at)
        if not isinstance(evidence, dict):
            raise ClosureArchiveError("evidence must be a dict")
        head = self.head(operation_id)
        if head is None or head["state"] in ("failed", "consumed"):
            raise ClosureArchiveError("closure operation cannot record failure")
        expected = self.next_stage(operation_id)
        if recovery_stage != expected:
            raise ClosureArchiveError(
                f"recovery_stage must be {expected!r}, got {recovery_stage!r}"
            )
        ordinal = len(self.history(operation_id)) + 1
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        cursor = self._conn.execute(
            "INSERT INTO initiative_closure_operation_journal ("
            "operation_id, initiative_id, workspace_id, ordinal, state, "
            "evidence_json, error_disposition, recovery_stage, actor_evidence, "
            "created_at) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)",
            (
                operation_id,
                initiative_id,
                workspace_id,
                ordinal,
                canonical,
                error_disposition,
                recovery_stage,
                actor_evidence,
                at,
            ),
        )
        row = self._conn.execute(
            self._SELECT + " WHERE event_id = ?", (cursor.lastrowid,)
        ).fetchone()
        if row is None:
            raise ClosureArchiveError("inserted failure event could not be read")
        return self._row(row)


@dataclass(frozen=True)
class _ArchiveAttachment:
    task_id: str
    attachment_id: int
    filename: str
    stored_path: str
    size: int


@dataclass(frozen=True)
class _ArchivePlan:
    operation_id: str
    initiative_id: str
    project_id: str
    board_slug: str
    closed_at: int
    primary_registration: _RepositoryRegistration
    archive_relative_path: str
    archive_worktree_path: str
    archive_branch: str
    tracker_export: dict[str, Any]
    attachments: tuple[_ArchiveAttachment, ...]
    workspace_id: Optional[str]
    members: tuple[_WorkspaceMember, ...]


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative_path(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClosureArchiveError(f"{name} must be a nonblank string")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ClosureArchiveError(f"{name} is not a safe relative path")
    return path.as_posix()


class _GitArchiveExecutor:
    """Create and remotely verify one immutable archive commit."""

    _SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")

    def __init__(self, timeout: float = 60) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ClosureArchiveError("timeout must be numeric")
        if timeout <= 0:
            raise ClosureArchiveError("timeout must be positive")
        self._timeout = timeout

    def _run(
        self, argv: list[str], cwd: str, *, check: bool = True, text: bool = True
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                shell=False,
                check=False,
                capture_output=True,
                text=text,
                encoding="utf-8" if text else None,
                errors="replace" if text else None,
                timeout=self._timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClosureArchiveError(f"command failed: {exc}") from exc
        if check and result.returncode != 0:
            raw = result.stderr or result.stdout or b""
            if isinstance(raw, bytes):
                detail = raw.decode("utf-8", errors="replace")
            else:
                detail = raw
            raise ClosureArchiveError(
                f"command {argv[0]!r} failed: {detail.strip()[:400]}"
            )
        return result

    def _git(self, cwd: str, *args: str) -> str:
        result = self._run(["git", *args], cwd)
        return (result.stdout or "").strip()

    def _git_bytes(self, cwd: str, *args: str) -> bytes:
        result = self._run(["git", *args], cwd, text=False)
        return result.stdout or b""

    @classmethod
    def _require_sha(cls, value: Any, name: str) -> str:
        if not isinstance(value, str) or cls._SHA_RE.fullmatch(value) is None:
            raise ClosureArchiveError(f"{name} is not a valid SHA")
        return value

    @staticmethod
    def _contained_path(root: Path, relative: str) -> Path:
        target = (root / Path(*PurePosixPath(relative).parts)).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError as exc:
            raise ClosureArchiveError(
                f"archive path {relative!r} escapes its controlled root"
            ) from exc
        return target

    def _remote_main(self, repository_root: str) -> str:
        self._git(repository_root, "fetch", "--no-tags", "origin")
        return self._require_sha(
            self._git(repository_root, "rev-parse", "refs/remotes/origin/main"),
            "origin/main",
        )

    def _remote_archive_manifest(
        self, plan: _ArchivePlan, expected: bytes
    ) -> Optional[dict[str, Any]]:
        remote = self._remote_main(plan.primary_registration.repository_root)
        spec = f"{remote}:{plan.archive_relative_path}/manifest.json"
        result = self._run(
            ["git", "show", spec],
            plan.primary_registration.repository_root,
            check=False,
            text=False,
        )
        if result.returncode != 0:
            return None
        observed = result.stdout or b""
        if observed != expected:
            raise ClosureArchiveError(
                "remote archive manifest exists with different content"
            )
        commit = self._git(
            plan.primary_registration.repository_root,
            "log",
            "-1",
            "--format=%H",
            remote,
            "--",
            f"{plan.archive_relative_path}/manifest.json",
        )
        return {
            "archive_commit_sha": self._require_sha(commit, "archive commit"),
            "archive_remote_sha": remote,
            "manifest_sha256": _sha256(expected),
            "archive_relative_path": plan.archive_relative_path,
        }

    def _ensure_worktree(self, plan: _ArchivePlan, remote_main: str) -> None:
        target = Path(plan.archive_worktree_path)
        controlled = Path(plan.primary_registration.controlled_worktree_root)
        try:
            target.resolve().relative_to(controlled.resolve())
        except ValueError as exc:
            raise ClosureArchiveError("archive worktree escapes controlled root") from exc
        repository_root = plan.primary_registration.repository_root
        if target.exists():
            common = self._git(str(target), "rev-parse", "--git-common-dir")
            common_path = (
                Path(common)
                if os.path.isabs(common)
                else (target / common)
            ).resolve()
            repo_common = Path(
                self._git(repository_root, "rev-parse", "--git-common-dir")
            )
            if not repo_common.is_absolute():
                repo_common = (Path(repository_root) / repo_common).resolve()
            if common_path != repo_common:
                raise ClosureArchiveError("archive worktree repository mismatch")
            branch = self._git(str(target), "symbolic-ref", "--short", "HEAD")
            if branch != plan.archive_branch:
                raise ClosureArchiveError("archive worktree branch mismatch")
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        branch_probe = self._run(
            [
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{plan.archive_branch}",
            ],
            repository_root,
            check=False,
        )
        if branch_probe.returncode == 0:
            argv = ["git", "worktree", "add", str(target), plan.archive_branch]
        elif branch_probe.returncode == 1:
            argv = [
                "git",
                "worktree",
                "add",
                "-b",
                plan.archive_branch,
                str(target),
                remote_main,
            ]
        else:
            raise ClosureArchiveError("failed to inspect archive branch")
        self._run(argv, repository_root)

    def _changed_paths(self, member: _WorkspaceMember) -> tuple[list[str], list[str]]:
        raw = self._git_bytes(
            member.target_path,
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=ACMRTUXB",
            member.required_base_sha,
            member.observed_head,
        )
        deleted_raw = self._git_bytes(
            member.target_path,
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=D",
            member.required_base_sha,
            member.observed_head,
        )
        changed = [
            _safe_relative_path(item.decode("utf-8"), "changed repository path")
            for item in raw.split(b"\0")
            if item
        ]
        deleted = [
            _safe_relative_path(item.decode("utf-8"), "deleted repository path")
            for item in deleted_raw.split(b"\0")
            if item
        ]
        return sorted(set(changed)), sorted(set(deleted))

    def _desired_files(
        self, plan: _ArchivePlan
    ) -> tuple[dict[str, bytes], list[dict[str, Any]], list[dict[str, Any]]]:
        tracker_bytes = _canonical_bytes(plan.tracker_export)
        files: dict[str, bytes] = {"tracker.json": tracker_bytes}
        entries: list[dict[str, Any]] = [
            {
                "archive_path": "tracker.json",
                "original_path": "tracker-database",
                "repository_identity": None,
                "source_commit": None,
                "sha256": _sha256(tracker_bytes),
            }
        ]
        dispositions: list[dict[str, Any]] = []

        for attachment in plan.attachments:
            source = Path(attachment.stored_path)
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise ClosureArchiveError(
                    f"attachment {attachment.attachment_id} cannot be read: {exc}"
                ) from exc
            if len(data) != attachment.size:
                raise ClosureArchiveError(
                    f"attachment {attachment.attachment_id} size mismatch"
                )
            safe_name = Path(attachment.filename).name
            if not safe_name or safe_name in (".", ".."):
                raise ClosureArchiveError("attachment filename is unsafe")
            relative = _safe_relative_path(
                f"attachments/{attachment.task_id}/"
                f"{attachment.attachment_id}-{safe_name}",
                "attachment archive path",
            )
            if relative in files:
                raise ClosureArchiveError("duplicate attachment archive path")
            files[relative] = data
            entries.append(
                {
                    "archive_path": relative,
                    "original_path": attachment.stored_path,
                    "repository_identity": None,
                    "source_commit": None,
                    "sha256": _sha256(data),
                }
            )

        for member in sorted(plan.members, key=lambda item: item.repository_identity):
            changed, deleted = self._changed_paths(member)
            for original in changed:
                data = self._git_bytes(
                    member.target_path,
                    "show",
                    f"{member.observed_head}:{original}",
                )
                relative = _safe_relative_path(
                    f"repositories/{member.repository_identity}/{original}",
                    "repository archive path",
                )
                if relative in files:
                    raise ClosureArchiveError("duplicate repository archive path")
                files[relative] = data
                entries.append(
                    {
                        "archive_path": relative,
                        "original_path": original,
                        "repository_identity": member.repository_identity,
                        "source_commit": member.observed_head,
                        "sha256": _sha256(data),
                    }
                )
            for original in deleted:
                dispositions.append(
                    {
                        "disposition": "deleted",
                        "original_path": original,
                        "repository_identity": member.repository_identity,
                        "source_commit": member.observed_head,
                    }
                )
        entries.sort(key=lambda item: item["archive_path"])
        dispositions.sort(
            key=lambda item: (
                item["repository_identity"], item["original_path"]
            )
        )
        return files, entries, dispositions

    def archive(self, plan: _ArchivePlan) -> dict[str, Any]:
        files, entries, dispositions = self._desired_files(plan)
        manifest = {
            "archive_format": "adrian-initiative-archive-v1",
            "initiative_id": plan.initiative_id,
            "project_id": plan.project_id,
            "board_slug": plan.board_slug,
            "closed_at": plan.closed_at,
            "files": entries,
            "dispositions": dispositions,
        }
        manifest_bytes = _canonical_bytes(manifest)
        remote_existing = self._remote_archive_manifest(plan, manifest_bytes)
        if remote_existing is not None:
            remote_existing["file_count"] = len(entries)
            return remote_existing

        remote_main = self._remote_main(plan.primary_registration.repository_root)
        self._ensure_worktree(plan, remote_main)
        archive_root = self._contained_path(
            Path(plan.archive_worktree_path), plan.archive_relative_path
        )
        archive_root.mkdir(parents=True, exist_ok=True)

        expected = set(files) | {"manifest.json"}
        existing = {
            path.relative_to(archive_root).as_posix()
            for path in archive_root.rglob("*")
            if path.is_file()
        }
        unexpected = existing - expected
        if unexpected:
            raise ClosureArchiveError(
                f"archive worktree contains unexpected files: {sorted(unexpected)}"
            )
        for relative, data in files.items():
            destination = self._contained_path(archive_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (archive_root / "manifest.json").write_bytes(manifest_bytes)

        worktree = plan.archive_worktree_path
        changed = [
            item
            for item in self._git(worktree, "diff", "--name-only").splitlines()
            if item
        ]
        untracked = [
            item
            for item in self._git(
                worktree, "ls-files", "--others", "--exclude-standard"
            ).splitlines()
            if item
        ]
        for path in changed + untracked:
            normalized = path.replace("\\", "/")
            if not (
                normalized == plan.archive_relative_path
                or normalized.startswith(plan.archive_relative_path + "/")
            ):
                raise ClosureArchiveError(
                    f"archive worktree has an unrelated change: {path}"
                )
        self._git(worktree, "add", "--", plan.archive_relative_path)
        staged = self._run(
            ["git", "diff", "--cached", "--quiet", "--exit-code"],
            worktree,
            check=False,
        )
        if staged.returncode == 1:
            self._git(
                worktree,
                "commit",
                "-m",
                f"Archive initiative {plan.initiative_id}",
            )
        elif staged.returncode != 0:
            raise ClosureArchiveError("failed to inspect staged archive")
        source_head = self._require_sha(
            self._git(worktree, "rev-parse", "HEAD"), "archive source head"
        )

        current_remote = self._remote_main(plan.primary_registration.repository_root)
        ancestor = self._run(
            ["git", "merge-base", "--is-ancestor", current_remote, source_head],
            worktree,
            check=False,
        )
        if ancestor.returncode == 1:
            self._git(worktree, "rebase", current_remote)
            source_head = self._require_sha(
                self._git(worktree, "rev-parse", "HEAD"), "rebased archive head"
            )
        elif ancestor.returncode != 0:
            raise ClosureArchiveError("failed to compare archive and remote main")

        self._git(worktree, "push", "origin", "HEAD:refs/heads/main")
        observed = self._remote_archive_manifest(plan, manifest_bytes)
        if observed is None:
            raise ClosureArchiveError("archive manifest absent after push")
        if observed["archive_remote_sha"] != source_head:
            raise ClosureArchiveError("remote archive head does not match pushed head")
        observed["file_count"] = len(entries)
        return observed

    def verify_archive(
        self, plan: _ArchivePlan, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        remote = self._remote_main(plan.primary_registration.repository_root)
        result = self._run(
            [
                "git",
                "show",
                f"{remote}:{plan.archive_relative_path}/manifest.json",
            ],
            plan.primary_registration.repository_root,
            check=False,
            text=False,
        )
        if result.returncode != 0:
            raise ClosureArchiveError("remote archive manifest is absent")
        manifest_bytes = result.stdout or b""
        if evidence.get("manifest_sha256") != _sha256(manifest_bytes):
            raise ClosureArchiveError("archive manifest evidence mismatch")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClosureArchiveError("remote archive manifest is invalid") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("initiative_id") != plan.initiative_id
            or manifest.get("project_id") != plan.project_id
            or manifest.get("closed_at") != plan.closed_at
        ):
            raise ClosureArchiveError("remote archive manifest identity mismatch")
        commit = self._git(
            plan.primary_registration.repository_root,
            "log",
            "-1",
            "--format=%H",
            remote,
            "--",
            f"{plan.archive_relative_path}/manifest.json",
        )
        return {
            "archive_commit_sha": self._require_sha(commit, "archive commit"),
            "archive_remote_sha": remote,
            "manifest_sha256": _sha256(manifest_bytes),
            "archive_relative_path": plan.archive_relative_path,
            "file_count": len(manifest.get("files", [])),
        }

    def retire_archive_worktree(self, plan: _ArchivePlan) -> None:
        target = Path(plan.archive_worktree_path)
        if not target.exists():
            return
        if self._git(str(target), "status", "--porcelain").strip():
            raise ClosureArchiveError("archive worktree is dirty after verification")
        self._git(
            plan.primary_registration.repository_root,
            "worktree",
            "remove",
            plan.archive_worktree_path,
        )
        if target.exists():
            raise ClosureArchiveError("archive worktree still exists after retirement")

    def retire_member(self, member: _WorkspaceMember) -> None:
        remote = self._remote_main(member.repository_root)
        contained = self._run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                member.observed_head,
                remote,
            ],
            member.repository_root,
            check=False,
        )
        if contained.returncode != 0:
            raise ClosureArchiveError(
                f"remote main does not contain {member.repository_identity!r} source"
            )
        target = Path(member.target_path)
        if not target.exists():
            return
        if self._git(str(target), "status", "--porcelain").strip():
            raise ClosureArchiveError(
                f"coordination member {member.repository_identity!r} is dirty"
            )
        if self._git(str(target), "rev-parse", "HEAD") != member.observed_head:
            raise ClosureArchiveError(
                f"coordination member {member.repository_identity!r} head changed"
            )
        if self._git(str(target), "symbolic-ref", "--short", "HEAD") != member.branch:
            raise ClosureArchiveError(
                f"coordination member {member.repository_identity!r} branch changed"
            )
        self._git(member.repository_root, "worktree", "remove", member.target_path)
        if target.exists():
            raise ClosureArchiveError(
                f"coordination member {member.repository_identity!r} still exists"
            )


class InitiativeArchiveController:
    """Resume-safe post-close archive and worktree-retirement controller."""

    _EXCLUDED_EXPORT_TABLES = frozenset(
        {
            "adrian_kanban_command_receipts",
            "adrian_kanban_notification_outbox",
            "adrian_kanban_rejection_audit",
            "adrian_kanban_schema_metadata",
            "adrian_kanban_migration_operations",
            "adrian_kanban_migration_map",
            "adrian_kanban_session_startup_records",
            "initiative_closure_operation_journal",
            "sqlite_sequence",
        }
    )

    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: _TrustedRepositoryRegistry,
        project_loader: Callable[[], list[dict[str, Any]]],
        executor: Optional[_GitArchiveExecutor] = None,
    ) -> None:
        if type(conn) is not sqlite3.Connection:
            raise ClosureArchiveError("conn must be a sqlite3.Connection")
        if not isinstance(registry, _TrustedRepositoryRegistry):
            raise ClosureArchiveError("registry must be _TrustedRepositoryRegistry")
        if not callable(project_loader):
            raise ClosureArchiveError("project_loader must be callable")
        if executor is None:
            executor = _GitArchiveExecutor()
        if not isinstance(executor, _GitArchiveExecutor):
            raise ClosureArchiveError("executor must be _GitArchiveExecutor")
        self._conn = conn
        self._registry = registry
        self._project_loader = project_loader
        self._executor = executor
        self._journal = ClosureOperationJournal(conn)
        self._store = CoordinationWorkspaceStore(conn)

    def _write(self, callback: Callable[[], Any]) -> Any:
        if self._conn.in_transaction:
            raise ClosureArchiveError("active transaction not allowed")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            value = callback()
            self._conn.execute("COMMIT")
            return value
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"encoding": "hex", "value": value.hex()}
        return value

    def _rows(
        self, table: str, where: str, parameters: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        rows = [
            {
                key: self._json_value(row[key])
                for key in row.keys()
            }
            for row in self._conn.execute(
                f'SELECT * FROM "{table}" WHERE {where}', parameters
            ).fetchall()
        ]
        rows.sort(key=lambda row: _canonical_bytes(row))
        return rows

    def _tracker_export(
        self, initiative_id: str, card_id: int
    ) -> tuple[dict[str, Any], tuple[_ArchiveAttachment, ...]]:
        task_ids = [
            row[0]
            for row in self._conn.execute(
                "SELECT task_id FROM adrian_kanban_cards "
                "WHERE initiative_id = ? AND task_id IS NOT NULL ORDER BY task_id",
                (initiative_id,),
            ).fetchall()
        ]
        workspace_ids = [
            row[0]
            for row in self._conn.execute(
                "SELECT workspace_id FROM segment_workspaces WHERE initiative_id = ? "
                "UNION SELECT workspace_id FROM initiative_coordination_workspaces "
                "WHERE initiative_id = ? ORDER BY workspace_id",
                (initiative_id, initiative_id),
            ).fetchall()
        ]
        tables = [
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            if row[0] not in self._EXCLUDED_EXPORT_TABLES
            and not row[0].startswith("sqlite_")
        ]
        exported: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            columns = {
                row[1]
                for row in self._conn.execute(f'PRAGMA table_info("{table}")')
            }
            rows: list[dict[str, Any]] = []
            if "initiative_id" in columns:
                rows = self._rows(table, "initiative_id = ?", (initiative_id,))
            elif "task_id" in columns and task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                rows = self._rows(
                    table, f"task_id IN ({placeholders})", tuple(task_ids)
                )
            elif "workspace_id" in columns and workspace_ids:
                placeholders = ",".join("?" for _ in workspace_ids)
                rows = self._rows(
                    table, f"workspace_id IN ({placeholders})", tuple(workspace_ids)
                )
            elif {"parent_id", "child_id"}.issubset(columns) and task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                rows = self._rows(
                    table,
                    f"parent_id IN ({placeholders}) OR child_id IN ({placeholders})",
                    tuple(task_ids) + tuple(task_ids),
                )
            elif table == "adrian_kanban_initiatives":
                rows = self._rows(table, "initiative_id = ?", (initiative_id,))
            if rows:
                exported[table] = rows

        cards = exported.get("adrian_kanban_cards", [])
        if not any(row.get("id") == card_id for row in cards):
            raise ClosureArchiveError("final initiative card missing from export")
        attachments = tuple(
            _ArchiveAttachment(
                task_id=row["task_id"],
                attachment_id=row["id"],
                filename=row["filename"],
                stored_path=row["stored_path"],
                size=row["size"],
            )
            for row in exported.get("task_attachments", [])
        )
        return (
            {
                "archive_format": "adrian-initiative-tracker-export-v1",
                "initiative_id": initiative_id,
                "tables": exported,
            },
            attachments,
        )

    def _coordination_record(
        self, initiative_id: str
    ) -> Optional[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT workspace_id, initiative_card_id, initiative_id, project_id, "
            "lifecycle_state, controller_binding_ref, binding_version, active, "
            "failure_detail, created_at, updated_at "
            "FROM initiative_coordination_workspaces WHERE initiative_id = ? "
            "ORDER BY active DESC, created_at DESC",
            (initiative_id,),
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        members = self._conn.execute(
            "SELECT workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, failure_detail, "
            "observed_at FROM initiative_coordination_workspace_members "
            "WHERE workspace_id = ? ORDER BY repository_identity",
            (row["workspace_id"],),
        ).fetchall()
        return {
            "workspace_id": row["workspace_id"],
            "initiative_card_id": row["initiative_card_id"],
            "initiative_id": row["initiative_id"],
            "project_id": row["project_id"],
            "lifecycle_state": row["lifecycle_state"],
            "controller_binding_ref": row["controller_binding_ref"],
            "binding_version": row["binding_version"],
            "active": row["active"],
            "failure_detail": row["failure_detail"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "members": [dict(member) for member in members],
        }

    def _resolve_project(
        self, project_id: Optional[str], board_slug: str
    ) -> dict[str, Any]:
        projects = self._project_loader()
        if not isinstance(projects, list):
            raise ClosureArchiveError("project_loader must return a list")
        if project_id is not None:
            matches = [item for item in projects if item.get("id") == project_id]
        else:
            matches = [
                item for item in projects if item.get("board_slug") == board_slug
            ]
        if len(matches) != 1:
            raise ClosureArchiveError(
                "closed initiative does not resolve to exactly one project"
            )
        project = matches[0]
        if not isinstance(project, dict):
            raise ClosureArchiveError("project record must be a dict")
        return project

    def _resolve_primary_registration(
        self, primary_path: Any
    ) -> _RepositoryRegistration:
        if not isinstance(primary_path, str) or not os.path.isabs(primary_path):
            raise ClosureArchiveError("project primary_path must be absolute")
        target = os.path.normcase(os.path.realpath(primary_path))
        matches = [
            registration
            for registration in self._registry._registrations
            if os.path.normcase(os.path.realpath(registration.repository_root))
            == target
        ]
        if len(matches) != 1:
            raise ClosureArchiveError(
                "project primary_path does not resolve to one trusted repository"
            )
        return matches[0]

    def _members(
        self, coordination: Optional[dict[str, Any]], initiative_id: str
    ) -> tuple[_WorkspaceMember, ...]:
        if coordination is None:
            return ()
        workspace_id = coordination["workspace_id"]
        members: list[_WorkspaceMember] = []
        for row in coordination["members"]:
            repository_identity = row["repository_identity"]
            try:
                registration = self._registry.lookup(repository_identity)
                target = os.path.normpath(
                    os.path.join(
                        registration.controlled_worktree_root,
                        row["relative_path"],
                    )
                )
                Path(target).resolve().relative_to(
                    Path(registration.controlled_worktree_root).resolve()
                )
                members.append(
                    _WorkspaceMember(
                        workspace_id=workspace_id,
                        repository_identity=repository_identity,
                        repository_root=registration.repository_root,
                        controlled_worktree_root=(
                            registration.controlled_worktree_root
                        ),
                        relative_path=row["relative_path"],
                        target_path=target,
                        branch=row["branch"],
                        required_base_sha=row["required_base_sha"],
                        observed_head=row["observed_head"],
                        member_state=row["member_state"],
                    )
                )
            except (ValueError, _WorkspaceRejected) as exc:
                raise ClosureArchiveError(
                    f"invalid coordination member {repository_identity!r}"
                ) from exc
        return tuple(members)

    def _build_plan(self, initiative_id: str) -> _ArchivePlan:
        cards = self._conn.execute(
            "SELECT id, board_slug, closed_at, record_version "
            "FROM adrian_kanban_cards WHERE initiative_id = ? "
            "AND card_type = 'initiative' AND task_id IS NULL",
            (initiative_id,),
        ).fetchall()
        if len(cards) != 1:
            raise ClosureArchiveError("initiative card is missing or ambiguous")
        card = cards[0]
        closed_at = card["closed_at"]
        if isinstance(closed_at, bool) or not isinstance(closed_at, int) or closed_at <= 0:
            raise ClosureArchiveError("initiative is not formally closed")
        coordination = self._coordination_record(initiative_id)
        project = self._resolve_project(
            coordination["project_id"] if coordination else None,
            card["board_slug"],
        )
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ClosureArchiveError("project id must be nonblank")
        primary = self._resolve_primary_registration(project.get("primary_path"))
        tracker_export, attachments = self._tracker_export(initiative_id, card["id"])
        members = self._members(coordination, initiative_id)
        archive_relative = _safe_relative_path(
            f"5-archive/{initiative_id}", "archive_relative_path"
        )
        archive_worktree = os.path.normpath(
            os.path.join(
                primary.controlled_worktree_root,
                initiative_id,
                "archive",
                primary.repository_identity,
            )
        )
        return _ArchivePlan(
            operation_id=f"initiative-close-{initiative_id}",
            initiative_id=initiative_id,
            project_id=project_id,
            board_slug=card["board_slug"],
            closed_at=closed_at,
            primary_registration=primary,
            archive_relative_path=archive_relative,
            archive_worktree_path=archive_worktree,
            archive_branch=f"initiative/{initiative_id}/archive",
            tracker_export=tracker_export,
            attachments=attachments,
            workspace_id=coordination["workspace_id"] if coordination else None,
            members=members,
        )

    def _append_state(
        self,
        plan: _ArchivePlan,
        stage: str,
        evidence: dict[str, Any],
        actor_evidence: str,
        at: int,
    ) -> dict[str, Any]:
        return self._write(
            lambda: self._journal.append_state(
                operation_id=plan.operation_id,
                initiative_id=plan.initiative_id,
                workspace_id=plan.workspace_id,
                state=stage,
                evidence=evidence,
                actor_evidence=actor_evidence,
                at=at,
            )
        )

    def _record_failure(
        self,
        plan: _ArchivePlan,
        stage: str,
        error: Exception,
        actor_evidence: str,
        at: int,
    ) -> None:
        if self._journal.head(plan.operation_id) is None:
            return
        try:
            self._write(
                lambda: self._journal.append_failed(
                    operation_id=plan.operation_id,
                    initiative_id=plan.initiative_id,
                    workspace_id=plan.workspace_id,
                    evidence={"failed_stage": stage},
                    error_disposition=str(error) or type(error).__name__,
                    recovery_stage=stage,
                    actor_evidence=actor_evidence,
                    at=at,
                )
            )
        except Exception:
            return

    def _successful_evidence(
        self, plan: _ArchivePlan, state: str
    ) -> dict[str, Any]:
        matches = [
            event
            for event in self._journal.history(plan.operation_id)
            if event["state"] == state
        ]
        if len(matches) != 1:
            raise ClosureArchiveError(
                f"closure journal has no unique {state!r} evidence"
            )
        return matches[0]["evidence"]

    def _verify_merged(self, plan: _ArchivePlan) -> dict[str, Any]:
        if plan.workspace_id is None:
            return {"workspace_id": None, "members": []}
        coordination = self._coordination_record(plan.initiative_id)
        if coordination is None or coordination["workspace_id"] != plan.workspace_id:
            raise ClosureArchiveError("coordination workspace changed")
        if coordination["lifecycle_state"] not in ("merged", "retired"):
            raise ClosureArchiveError("coordination workspace is not merged")
        evidence_members = []
        for member in coordination["members"]:
            if member["member_state"] not in ("merged", "retired"):
                raise ClosureArchiveError("coordination member is not merged")
            evidence_members.append(
                {
                    "repository_identity": member["repository_identity"],
                    "required_base_sha": member["required_base_sha"],
                    "observed_head": member["observed_head"],
                }
            )
        if not evidence_members:
            raise ClosureArchiveError("coordination workspace has no members")
        return {
            "workspace_id": plan.workspace_id,
            "members": evidence_members,
        }

    def _retire(self, plan: _ArchivePlan, at: int) -> dict[str, Any]:
        archive_evidence = self._successful_evidence(plan, "archive_verified")
        self._executor.verify_archive(plan, archive_evidence)
        self._executor.retire_archive_worktree(plan)
        if plan.workspace_id is None:
            return {"workspace_id": None, "retired_members": []}

        retired: list[str] = []
        for member in plan.members:
            if member.member_state == "retired":
                retired.append(member.repository_identity)
                continue
            if member.member_state != "merged":
                raise ClosureArchiveError(
                    f"coordination member {member.repository_identity!r} is not merged"
                )
            self._executor.retire_member(member)
            try:
                self._store.transition_member(
                    plan.workspace_id,
                    member.repository_identity,
                    expected_state="merged",
                    to_state="retired",
                    at=at,
                )
            except CoordinationWorkspaceError as exc:
                raise ClosureArchiveError(
                    f"failed to retire {member.repository_identity!r}: {exc}"
                ) from exc
            retired.append(member.repository_identity)

        coordination = self._coordination_record(plan.initiative_id)
        if coordination is None:
            raise ClosureArchiveError("coordination workspace disappeared")
        if coordination["lifecycle_state"] == "merged":
            try:
                self._store.transition_workspace(
                    plan.workspace_id,
                    expected_state="merged",
                    to_state="retired",
                    at=at,
                )
            except CoordinationWorkspaceError as exc:
                raise ClosureArchiveError(
                    f"failed to retire coordination workspace: {exc}"
                ) from exc
        elif coordination["lifecycle_state"] != "retired":
            raise ClosureArchiveError("coordination workspace retirement mismatch")
        return {
            "workspace_id": plan.workspace_id,
            "retired_members": sorted(retired),
        }

    def finalize(
        self, *, initiative_id: str, actor_evidence: str, at: int
    ) -> dict[str, Any]:
        if self._conn.in_transaction:
            raise ClosureArchiveError("active transaction not allowed")
        if not isinstance(initiative_id, str) or not initiative_id.strip():
            raise ClosureArchiveError("initiative_id must be a nonblank string")
        if not isinstance(actor_evidence, str) or not actor_evidence.strip():
            raise ClosureArchiveError("actor_evidence must be a nonblank string")
        if isinstance(at, bool) or not isinstance(at, int) or at <= 0:
            raise ClosureArchiveError("at must be a positive integer")

        plan = self._build_plan(initiative_id.strip())
        while True:
            stage = self._journal.next_stage(plan.operation_id)
            if stage is None:
                history = self._journal.history(plan.operation_id)
                return {
                    "state": "consumed",
                    "operation_id": plan.operation_id,
                    "archive": self._successful_evidence(
                        plan, "archive_verified"
                    ),
                    "history": history,
                }
            try:
                if stage == "prepared":
                    evidence = {
                        "archive_relative_path": plan.archive_relative_path,
                        "board_slug": plan.board_slug,
                        "closed_at": plan.closed_at,
                        "project_id": plan.project_id,
                    }
                elif stage == "merged":
                    evidence = self._verify_merged(plan)
                elif stage == "closed":
                    evidence = {
                        "closed_at": plan.closed_at,
                        "tracker_sha256": _sha256(
                            _canonical_bytes(plan.tracker_export)
                        ),
                    }
                elif stage == "archive_verified":
                    evidence = self._executor.archive(plan)
                elif stage == "retired":
                    evidence = self._retire(plan, at)
                elif stage == "consumed":
                    archive = self._successful_evidence(plan, "archive_verified")
                    self._executor.verify_archive(plan, archive)
                    evidence = {
                        "archive_commit_sha": archive["archive_commit_sha"],
                        "manifest_sha256": archive["manifest_sha256"],
                        "closed_at": plan.closed_at,
                    }
                else:
                    raise ClosureArchiveError(f"unknown closure stage {stage!r}")
                self._append_state(plan, stage, evidence, actor_evidence, at)
            except Exception as exc:
                self._record_failure(plan, stage, exc, actor_evidence, at)
                if isinstance(exc, ClosureArchiveError):
                    raise
                raise ClosureArchiveError(
                    f"closure archive stage {stage!r} failed: {exc}"
                ) from exc


def finalize_initiative_archive(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    project_loader: Callable[[], list[dict[str, Any]]],
    *,
    initiative_id: str,
    actor_evidence: str,
    at: int,
) -> dict[str, Any]:
    return InitiativeArchiveController(
        conn, registry, project_loader
    ).finalize(
        initiative_id=initiative_id,
        actor_evidence=actor_evidence,
        at=at,
    )

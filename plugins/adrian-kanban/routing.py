from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as _kb
from hermes_cli import projects_db as _projects
from writegate import registry as _writegate


class RouteResolutionRejected(RuntimeError):
    pass


def _canonicalize_directory(path: Any) -> str:
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise RouteResolutionRejected(f"unresolvable path: {path!r}") from exc
    if not resolved.is_dir():
        raise RouteResolutionRejected(f"not a directory: {path!r}")
    return os.path.normcase(str(resolved))


class ModelToolBoardResolver:
    def __init__(
        self,
        database_path: str,
        *,
        registry_getter: Any = None,
        projects_connector: Any = None,
        board_lister: Any = None,
    ) -> None:
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("database_path must be a nonblank string")
        raw_path = Path(database_path).expanduser()
        if not raw_path.is_absolute():
            raise ValueError("database_path must be absolute")
        self._database_path = str(raw_path.resolve())

        self._registry_getter = registry_getter or _writegate.get_registry
        self._projects_connector = (
            projects_connector or _projects.connect_closing
        )
        self._board_lister = board_lister or (
            lambda: _kb.list_boards(include_archived=False)
        )
        for name, dependency in (
            ("registry_getter", self._registry_getter),
            ("projects_connector", self._projects_connector),
            ("board_lister", self._board_lister),
        ):
            if not callable(dependency):
                raise ValueError(f"{name} must be callable")

    def __call__(
        self,
        operation: str,
        public_args: dict[str, Any],
        runtime_fields: dict[str, Any],
    ) -> tuple[str, str | None, str]:
        if not isinstance(operation, str) or not operation.strip():
            raise RouteResolutionRejected("operation must be a nonblank string")
        if type(public_args) is not dict or type(runtime_fields) is not dict:
            raise RouteResolutionRejected(
                "public_args and runtime_fields must be exact dictionaries"
            )
        session_id = runtime_fields.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise RouteResolutionRejected(
                "runtime session_id must be a nonblank string"
            )

        try:
            registry = self._registry_getter()
            binding = registry.get_active_binding(session_id.strip())
        except Exception as exc:
            raise RouteResolutionRejected(
                f"failed to resolve active Write-Gate binding: {exc}"
            ) from exc
        if binding is None:
            raise RouteResolutionRejected("no active binding for session")
        actor_profile = getattr(binding, "profile", None)
        if not (type(actor_profile) is str and actor_profile.strip()):
            raise RouteResolutionRejected(
                "binding profile must be a nonblank string"
            )
        actor_profile = actor_profile.strip()
        worktree_path = getattr(binding, "worktree_path", None)
        if not isinstance(worktree_path, str) or not worktree_path.strip():
            raise RouteResolutionRejected(
                "binding worktree_path must be a nonblank string"
            )
        canonical_worktree = _canonicalize_directory(worktree_path)

        resolved_project = None
        if getattr(binding, "producer", None) == "dispatcher":
            resolved_board = getattr(binding, "board", None)
            if not isinstance(resolved_board, str) or not resolved_board.strip():
                raise RouteResolutionRejected(
                    "dispatcher binding must have a nonblank board"
                )
            resolved_board = resolved_board.strip()
        else:
            try:
                with self._projects_connector() as project_conn:
                    binding_project = getattr(binding, "project", None)
                    if isinstance(binding_project, str) and binding_project.strip():
                        resolved_project = _projects.get_project(
                            project_conn,
                            binding_project.strip(),
                        )
                        if resolved_project is None:
                            raise RouteResolutionRejected(
                                f"unknown project: {binding_project!r}"
                            )
                    else:
                        resolved_project = _projects.project_for_path(
                            project_conn,
                            worktree_path,
                        )
            except RouteResolutionRejected:
                raise
            except Exception as exc:
                raise RouteResolutionRejected(
                    f"failed to resolve project for active worktree: {exc}"
                ) from exc

            project_board = (
                getattr(resolved_project, "board_slug", None)
                if resolved_project is not None
                else None
            )
            if isinstance(project_board, str) and project_board.strip():
                resolved_board = project_board.strip()
            else:
                matches: list[str] = []
                try:
                    boards = self._board_lister()
                except Exception as exc:
                    raise RouteResolutionRejected(
                        f"failed to list boards: {exc}"
                    ) from exc
                for board in boards:
                    if not isinstance(board, dict):
                        continue
                    default_workdir = board.get("default_workdir")
                    if not isinstance(default_workdir, str):
                        continue
                    try:
                        board_workdir = _canonicalize_directory(default_workdir)
                    except RouteResolutionRejected:
                        continue
                    slug = board.get("slug")
                    if (
                        board_workdir == canonical_worktree
                        and isinstance(slug, str)
                        and slug.strip()
                    ):
                        matches.append(slug.strip())
                if len(matches) != 1:
                    raise RouteResolutionRejected(
                        "no board matches the active worktree"
                        if not matches
                        else "multiple boards match the active worktree"
                    )
                resolved_board = matches[0]

            binding_board = getattr(binding, "board", None)
            if (
                isinstance(binding_board, str)
                and binding_board.strip()
                and binding_board.strip() != resolved_board
            ):
                raise RouteResolutionRejected(
                    f"binding board {binding_board!r} does not match "
                    f"resolved board {resolved_board!r}"
                )

        public_project = public_args.get("project")
        if public_project is not None:
            if not isinstance(public_project, str) or not public_project.strip():
                raise RouteResolutionRejected(
                    "public project must be a nonblank string"
                )
            if resolved_project is None:
                raise RouteResolutionRejected(
                    "cannot verify public project against the active binding"
                )
            if public_project.strip() not in {
                getattr(resolved_project, "id", None),
                getattr(resolved_project, "slug", None),
            }:
                raise RouteResolutionRejected(
                    f"public project {public_project!r} does not match "
                    "the active workspace project"
                )

        task_target = (
            public_args.get("child_id")
            if operation == "kanban_link"
            else public_args.get("task_id")
        )
        workspace_id = None
        if isinstance(task_target, str) and task_target.strip():
            try:
                with sqlite3.connect(self._database_path) as conn:
                    row = conn.execute(
                        "SELECT workspace_id FROM task_lifecycle_contracts "
                        "WHERE task_id = ?",
                        (task_target.strip(),),
                    ).fetchone()
                if row is not None and row[0] is not None:
                    if not isinstance(row[0], str) or not row[0].strip():
                        raise RouteResolutionRejected(
                            "lifecycle workspace_id is invalid"
                        )
                    workspace_id = row[0]
            except RouteResolutionRejected:
                raise
            except Exception as exc:
                raise RouteResolutionRejected(
                    f"failed to query lifecycle workspace: {exc}"
                ) from exc

        return resolved_board, workspace_id, actor_profile


def resolve_expected_version(
    conn: sqlite3.Connection,
    operation: str,
    target: str,
    payload: dict[str, Any],
) -> int:
    from .commands import (
        INITIATIVE_OPERATIONS,
        ORDINARY_TASK_OPERATIONS,
        READ_ONLY_OPERATIONS,
    )

    if type(conn) is not sqlite3.Connection:
        raise RouteResolutionRejected("conn must be a sqlite3.Connection")
    if operation in READ_ONLY_OPERATIONS or operation not in (
        INITIATIVE_OPERATIONS | ORDINARY_TASK_OPERATIONS
    ):
        raise RouteResolutionRejected("operation is not a mutation")
    if not isinstance(target, str) or not target.strip():
        raise RouteResolutionRejected("target must be a nonblank string")
    if type(payload) is not dict:
        raise RouteResolutionRejected("payload must be a dict")
    board = payload.get("board")
    if not isinstance(board, str) or not board.strip():
        raise RouteResolutionRejected("payload board must be nonblank")

    target = target.strip()
    board = board.strip()
    if operation in {"kanban_create", "kanban_create_initiative"}:
        if operation == "kanban_create":
            row = conn.execute(
                "SELECT 1 FROM adrian_kanban_cards WHERE task_id = ?",
                (target,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM adrian_kanban_cards "
                "WHERE initiative_id = ? AND task_id IS NULL",
                (target,),
            ).fetchone()
        if row is not None:
            raise RouteResolutionRejected("target card already exists")
        return 0

    if operation in INITIATIVE_OPERATIONS:
        rows = conn.execute(
            "SELECT board_slug, record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND task_id IS NULL",
            (target,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT board_slug, record_version FROM adrian_kanban_cards "
            "WHERE task_id = ?",
            (target,),
        ).fetchall()
    if len(rows) != 1:
        raise RouteResolutionRejected(
            f"expected exactly one target card, found {len(rows)}"
        )
    if rows[0][0] != board:
        raise RouteResolutionRejected(
            f"card board {rows[0][0]!r} does not match resolved board {board!r}"
        )
    version = rows[0][1]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise RouteResolutionRejected(
            "record_version must be a nonnegative integer"
        )
    return version


__all__ = [
    "RouteResolutionRejected",
    "ModelToolBoardResolver",
    "resolve_expected_version",
]

"""Controlled one-time coordination-workspace backfill for open initiatives."""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

from .coordination_workspace import (
    CoordinationWorkspaceError,
    CoordinationWorkspaceStore,
)
from .workspace import _TrustedRepositoryRegistry

__all__ = [
    "CoordinationBackfillError",
    "apply_coordination_backfill",
    "plan_coordination_backfill",
    "run_coordination_backfill",
]


class CoordinationBackfillError(Exception):
    """Raised when the complete backfill cannot be safely preflighted."""


def _project_mapping(projects: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(projects, list):
        raise CoordinationBackfillError("projects must be a list")
    mapping: dict[str, dict[str, Any]] = {}
    for project in projects:
        if not isinstance(project, dict):
            raise CoordinationBackfillError("project entries must be dictionaries")
        project_id = project.get("id")
        board_slug = project.get("board_slug")
        primary_path = project.get("primary_path")
        if not isinstance(project_id, str) or not project_id.strip():
            raise CoordinationBackfillError("project id must be nonblank")
        if board_slug is None:
            continue
        if not isinstance(board_slug, str) or not board_slug.strip():
            raise CoordinationBackfillError(
                f"project {project_id!r} board_slug must be nonblank or null"
            )
        if not isinstance(primary_path, str) or not os.path.isabs(primary_path):
            raise CoordinationBackfillError(
                f"project {project_id!r} primary_path must be absolute"
            )
        if board_slug in mapping:
            raise CoordinationBackfillError(
                f"board {board_slug!r} resolves to multiple projects"
            )
        mapping[board_slug] = project
    return mapping


def _repository_identity(
    registry: _TrustedRepositoryRegistry, primary_path: str
) -> str:
    expected = os.path.normcase(os.path.realpath(primary_path))
    matches = [
        registration.repository_identity
        for registration in registry._registrations
        if os.path.normcase(os.path.realpath(registration.repository_root))
        == expected
    ]
    if len(matches) != 1:
        raise CoordinationBackfillError(
            f"primary_path {primary_path!r} does not resolve to one trusted repository"
        )
    return matches[0]


def plan_coordination_backfill(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    projects: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Preflight the complete backfill without changing Tracker state."""
    if type(conn) is not sqlite3.Connection:
        raise CoordinationBackfillError("conn must be a sqlite3.Connection")
    if not isinstance(registry, _TrustedRepositoryRegistry):
        raise CoordinationBackfillError("registry must be _TrustedRepositoryRegistry")
    if conn.in_transaction:
        raise CoordinationBackfillError("active transaction not allowed")
    by_board = _project_mapping(projects)
    cards = conn.execute(
        "SELECT id, initiative_id, board_slug FROM adrian_kanban_cards "
        "WHERE card_type='initiative' AND task_id IS NULL AND closed_at IS NULL "
        "ORDER BY initiative_id"
    ).fetchall()
    skipped_closed = [
        {"initiative_id": row[0], "reason": "formally_closed"}
        for row in conn.execute(
            "SELECT initiative_id FROM adrian_kanban_cards "
            "WHERE card_type='initiative' AND task_id IS NULL "
            "AND closed_at IS NOT NULL ORDER BY initiative_id"
        ).fetchall()
    ]
    planned: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []
    for card in cards:
        initiative_id = card["initiative_id"]
        board_slug = card["board_slug"]
        project = by_board.get(board_slug)
        if project is None:
            raise CoordinationBackfillError(
                f"initiative {initiative_id!r} has no project for board {board_slug!r}"
            )
        repository_identity = _repository_identity(
            registry, project["primary_path"]
        )
        expected = {
            "initiative_card_id": card["id"],
            "initiative_id": initiative_id,
            "project_id": project["id"],
            "repository_identity": repository_identity,
            "controller_binding_ref": f"tracker:{board_slug}:{initiative_id}",
        }
        workspace_rows = conn.execute(
            "SELECT workspace_id, initiative_card_id, project_id, "
            "controller_binding_ref, active FROM initiative_coordination_workspaces "
            "WHERE initiative_id = ? ORDER BY active DESC, created_at DESC",
            (initiative_id,),
        ).fetchall()
        if not workspace_rows:
            planned.append(expected)
            continue
        active_rows = [row for row in workspace_rows if row["active"] == 1]
        if len(active_rows) != 1:
            raise CoordinationBackfillError(
                f"initiative {initiative_id!r} has no unique active workspace"
            )
        workspace = active_rows[0]
        if (
            workspace["workspace_id"] != f"coord-{initiative_id}"
            or workspace["initiative_card_id"] != card["id"]
            or workspace["project_id"] != project["id"]
            or workspace["controller_binding_ref"]
            != expected["controller_binding_ref"]
        ):
            raise CoordinationBackfillError(
                f"initiative {initiative_id!r} existing workspace does not agree"
            )
        member = conn.execute(
            "SELECT relative_path, branch FROM "
            "initiative_coordination_workspace_members "
            "WHERE workspace_id = ? AND repository_identity = ?",
            (workspace["workspace_id"], repository_identity),
        ).fetchone()
        if member is None or tuple(member) != (
            f"{initiative_id}/coordination/{repository_identity}",
            f"initiative/{initiative_id}/coordination/{repository_identity}",
        ):
            raise CoordinationBackfillError(
                f"initiative {initiative_id!r} primary workspace member does not agree"
            )
        existing.append(
            {
                **expected,
                "workspace_id": workspace["workspace_id"],
            }
        )
    return {
        "planned": planned,
        "existing": existing,
        "skipped_closed": skipped_closed,
    }


def apply_coordination_backfill(
    conn: sqlite3.Connection,
    registry: _TrustedRepositoryRegistry,
    projects: list[dict[str, Any]],
    *,
    at: int,
) -> dict[str, list[dict[str, Any]]]:
    """Create only the fully preflighted planned primary members."""
    if isinstance(at, bool) or not isinstance(at, int) or at <= 0:
        raise CoordinationBackfillError("at must be a positive integer")
    plan = plan_coordination_backfill(conn, registry, projects)
    store = CoordinationWorkspaceStore(conn)
    created: list[dict[str, Any]] = []
    try:
        for item in plan["planned"]:
            workspace = store.plan_or_read(
                initiative_id=item["initiative_id"],
                project_id=item["project_id"],
                repository_identity=item["repository_identity"],
                controller_binding_ref=item["controller_binding_ref"],
                planned_at=at,
            )
            created.append(
                {
                    **item,
                    "workspace_id": workspace["workspace_id"],
                    "lifecycle_state": workspace["lifecycle_state"],
                }
            )
    except CoordinationWorkspaceError as exc:
        raise CoordinationBackfillError(f"backfill apply failed: {exc}") from exc
    return {
        "created": created,
        "existing": plan["existing"],
        "skipped_closed": plan["skipped_closed"],
    }


def run_coordination_backfill(
    database_path: str,
    registry: _TrustedRepositoryRegistry,
    projects: list[dict[str, Any]],
    *,
    apply: bool = False,
    at: int | None = None,
) -> dict[str, Any]:
    """Run the maintenance backfill against one explicit Tracker database."""
    if not isinstance(database_path, str) or not database_path.strip():
        raise CoordinationBackfillError("database_path must be nonblank")
    if not os.path.isabs(database_path):
        raise CoordinationBackfillError("database_path must be absolute")
    if type(apply) is not bool:
        raise CoordinationBackfillError("apply must be a boolean")
    if at is not None and (
        isinstance(at, bool) or not isinstance(at, int) or at <= 0
    ):
        raise CoordinationBackfillError("at must be a positive integer or null")

    conn = sqlite3.connect(os.path.abspath(database_path))
    conn.row_factory = sqlite3.Row
    try:
        if not apply:
            return {
                "mode": "plan",
                **plan_coordination_backfill(conn, registry, projects),
            }
        effective_at = at if at is not None else int(time.time())
        return {
            "mode": "apply",
            **apply_coordination_backfill(
                conn,
                registry,
                projects,
                at=effective_at,
            ),
        }
    except sqlite3.Error as exc:
        raise CoordinationBackfillError(
            f"Tracker database backfill failed: {exc}"
        ) from exc
    finally:
        conn.close()

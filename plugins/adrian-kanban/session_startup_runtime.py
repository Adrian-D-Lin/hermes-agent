from __future__ import annotations

import os
import sqlite3
from typing import Any, Callable, Dict, List

import hermes_cli.profiles as profiles
import hermes_cli.projects_db as projects_db

from .projections import list_projection
from .session_startup import SessionStartupController
from .session_startup_anchor import SessionStartupAnchorResolver
from .session_startup_creation import SessionStartupInitiativeCreationCoordinator
from .store import AdmittedStore


def load_projects() -> List[Dict[str, Any]]:
    with projects_db.connect_closing() as conn:
        projects = projects_db.list_projects(conn, include_archived=True)
        return [
            {
                "id": p.id,
                "name": p.name,
                "board_slug": p.board_slug,
                "primary_path": p.primary_path,
                "archived": p.archived,
            }
            for p in projects
        ]


def build_initiative_loader(database_path: str) -> Callable[[str], List[Dict[str, Any]]]:
    if not isinstance(database_path, str) or not database_path.strip():
        raise ValueError(f"database_path must be a nonblank string, got {database_path!r}")
    if not os.path.isabs(database_path):
        raise ValueError(f"database_path must be an absolute path, got {database_path!r}")
    normalized_path = os.path.abspath(database_path)

    def load(board_slug: str) -> List[Dict[str, Any]]:
        if not isinstance(board_slug, str) or not board_slug.strip():
            raise ValueError(f"board_slug must be a nonblank string, got {board_slug!r}")

        conn = sqlite3.connect(normalized_path)
        try:
            conn.row_factory = sqlite3.Row
            try:
                result = list_projection(conn, board=board_slug)
            except Exception as exc:
                raise RuntimeError(f"failed to load initiatives for board {board_slug!r}") from exc
            if not isinstance(result, dict):
                raise TypeError("projection result must be a dict")
            initiatives = result.get("initiatives")
            if not isinstance(initiatives, list):
                raise TypeError("projection result must contain a list-valued 'initiatives' key")
            return initiatives
        finally:
            conn.close()

    return load


def build_session_startup_hook(
    database_path: str,
    trusted_registry: Any,
    command_boundary: Any,
) -> Callable[..., Dict[str, Any]]:
    if not isinstance(database_path, str) or not database_path.strip():
        raise ValueError(
            f"database_path must be a nonblank string, got {database_path!r}"
        )
    if not os.path.isabs(database_path):
        raise ValueError(
            f"database_path must be an absolute path, got {database_path!r}"
        )
    normalized_path = os.path.abspath(database_path)

    construction_error: str | None = None
    try:
        initiative_loader = build_initiative_loader(normalized_path)
        anchor_resolver = SessionStartupAnchorResolver(
            tracker_database_path=normalized_path,
            registry=trusted_registry,
        )
        creation_coordinator = SessionStartupInitiativeCreationCoordinator(
            normalized_path,
            command_boundary,
        )
    except Exception as exc:
        construction_error = (
            f"session startup dependency construction failed: {exc}"
        )
        initiative_loader = None
        anchor_resolver = None
        creation_coordinator = None

    def callback(
        session_id: str | None = None,
        user_message: str | None = None,
        conversation_history: List[Dict[str, Any]] | None = None,
        first_turn: bool = False,
        parent_session_id: str | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if parent_session_id and parent_session_id.strip():
            return {"action": "allow"}
        if os.environ.get("HERMES_KANBAN_TASK", "").strip():
            return {"action": "allow"}

        try:
            profile = profiles.get_active_profile_name() or "default"
        except Exception:
            profile = "default"
        if profile != "default":
            return {"action": "allow"}

        if construction_error is not None:
            return {
                "action": "fail_closed",
                "response": f"[Session Startup] {construction_error}",
            }

        try:
            with AdmittedStore(database_path=normalized_path) as store:
                controller = SessionStartupController(
                    store=store,
                    project_loader=load_projects,
                    initiative_loader=initiative_loader,
                    anchor_resolver=anchor_resolver.resolve,
                    anchor_revalidator=anchor_resolver.revalidate,
                    anchor_replacer=anchor_resolver.replace,
                    initiative_creation_coordinator=creation_coordinator,
                )
                return controller.handle(
                    session_id=session_id,
                    user_message=user_message,
                    conversation_history=conversation_history,
                    first_turn=first_turn,
                    parent_session_id=parent_session_id,
                )
        except Exception as exc:
            return {
                "action": "fail_closed",
                "response": f"[Session Startup] runtime error: {exc}",
            }

    return callback


__all__ = [
    "build_initiative_loader",
    "build_session_startup_hook",
    "load_projects",
]

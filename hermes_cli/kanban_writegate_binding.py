"""Write-Gate identity handshake for dispatched Kanban workers.

Hermes v0.21.3 split the Kanban implementation into focused modules.  This
module preserves Adrian's narrow pre-spawn contract without restoring the old
monolithic ``kanban_db.py`` implementation: the dispatcher mints one worker
session identity, persists it on the exact run, binds it centrally, and the
worker later accepts it only when every trusted lineage marker agrees.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Optional
import uuid

from hermes_cli.kanban_db_connect import write_txn


class WriteGatePreSpawnError(Exception):
    """The worker must not spawn because its authority binding is unusable."""


def _host():
    from hermes_cli import kanban_db

    return kanban_db


def _writegate_binding_enabled() -> bool:
    """Return whether the configured Write-Gate handshake is active."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        security = config.get("security") if isinstance(config, dict) else None
        if not isinstance(security, dict):
            return False
        write_gate = security.get("write_gate")
        return bool(
            isinstance(write_gate, dict) and write_gate.get("enabled", False)
        )
    except Exception:
        return False


def _ensure_writegate_importable() -> bool:
    """Ensure the repository-root ``writegate`` package can be imported."""
    try:
        if "writegate" in sys.modules or importlib.util.find_spec("writegate"):
            return True
        here = Path(__file__).resolve().parent
        for candidate in (here, *here.parents):
            if (candidate / "writegate").is_dir():
                root = str(candidate)
                if root not in sys.path:
                    sys.path.insert(0, root)
                break
        importlib.import_module("writegate")
        return True
    except Exception:
        return False


def prepare_worker_launch(
    conn: sqlite3.Connection,
    task,
    workspace: str,
    *,
    board: Optional[str] = None,
    resolved_branch_name: Optional[str] = None,
) -> str:
    """Persist and, when enabled, bind one identity before worker spawn."""
    run_id = task.current_run_id
    if run_id is None:
        return ""

    worker_session_id = _generate_worker_session_id(task.id, int(run_id))
    _persist_worker_session_id(conn, task.id, int(run_id), worker_session_id)

    if _writegate_binding_enabled():
        _ensure_writegate_importable()
        try:
            _create_and_verify_central_binding(
                task,
                workspace,
                worker_session_id,
                board,
                resolved_branch_name,
            )
        except Exception as exc:
            _abandon_pre_spawn_binding(worker_session_id)
            raise WriteGatePreSpawnError(
                f"WriteGate: pre-spawn binding handshake failed: {exc}"
            ) from exc
    return worker_session_id


def _generate_worker_session_id(task_id: str, run_id: int) -> str:
    """Mint the ordinary Hermes ``timestamp_uuid6`` session-id shape."""
    del task_id, run_id
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"{timestamp}_{uuid.uuid4().hex[:6]}"


def _persist_worker_session_id(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    worker_session_id: str,
) -> None:
    """Commit the identity on the exact run before the child can start."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE task_runs SET worker_session_id = ? "
            "WHERE id = ? AND task_id = ?",
            (worker_session_id, int(run_id), task_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                "WriteGate: expected exactly one task_runs row for run "
                f"{run_id} / task {task_id}, found {cur.rowcount}"
            )


def _create_and_verify_central_binding(
    task,
    workspace: str,
    worker_session_id: str,
    board: Optional[str],
    resolved_branch_name: Optional[str],
) -> None:
    from writegate import registry

    reg = registry.get_registry()
    record = reg.create_binding(
        session_id=worker_session_id,
        worktree_path=str(workspace),
        project=task.project_id or None,
        initiative=task.id,
        board=board,
        git_branch=resolved_branch_name or task.branch_name or None,
        profile=task.assignee,
        producer=registry.PRODUCER_DISPATCHER,
        event=registry.EVENT_DISPATCH,
    )
    verify = reg.get_active_binding(worker_session_id)
    if verify is None or verify.id != record.id:
        raise WriteGatePreSpawnError(
            "WriteGate: pre-spawn binding could not be verified for "
            f"session {worker_session_id}"
        )


def _abandon_pre_spawn_binding(worker_session_id: str) -> None:
    if not worker_session_id:
        return
    try:
        from writegate import registry

        registry.get_registry().mark_binding_abandoned(worker_session_id)
    except Exception:
        pass


def _trusted_worker_session_id(
    task_id: str,
    run_id: int,
    *,
    profile: Optional[str] = None,
    workspace: Optional[str] = None,
    board: Optional[str] = None,
) -> Optional[str]:
    """Return the persisted worker identity only when all lineage agrees."""
    host = _host()
    try:
        db_path = host.kanban_db_path(board=board)
        if not db_path.exists():
            return None
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception:
        return None

    try:
        row = conn.execute(
            "SELECT id, task_id, worker_session_id FROM task_runs "
            "WHERE id = ? AND task_id = ?",
            (int(run_id), task_id),
        ).fetchone()
        if row is None or not row["worker_session_id"]:
            return None
        stored_id = row["worker_session_id"]
        task = host.get_task(conn, task_id)
        if task is None:
            return None
        if profile and (
            task.assignee is None or host._canonical_assignee(task.assignee) != profile
        ):
            return None
        if workspace and (
            task.workspace_path is None
            or os.path.realpath(task.workspace_path) != os.path.realpath(workspace)
        ):
            return None
    except Exception:
        return None
    finally:
        conn.close()

    if board:
        try:
            slug = host._normalize_board_slug(board)
            if slug is None:
                return None
        except Exception:
            return None

    try:
        from writegate import registry

        binding = registry.get_registry().get_active_binding(stored_id)
    except Exception:
        return None
    if binding is None or not binding.is_active:
        return None
    if workspace and (
        binding.worktree_path is None
        or os.path.realpath(binding.worktree_path) != os.path.realpath(workspace)
    ):
        return None
    if board and binding.board not in (board, host._normalize_board_slug(board)):
        return None
    if binding.initiative not in (task_id, None):
        return None
    if profile and binding.profile not in (profile, task.assignee):
        return None
    return stored_id

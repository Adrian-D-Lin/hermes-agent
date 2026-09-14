"""Coordination workspace store and deterministic planner."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["CoordinationWorkspaceStore", "CoordinationWorkspaceError"]


class CoordinationWorkspaceError(Exception):
    """Module-specific error for coordination workspace operations."""


_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_component(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise CoordinationWorkspaceError(f"{name} must be a string")
    if not value or value.strip() == "":
        raise CoordinationWorkspaceError(f"{name} must be nonblank")
    if value in (".", ".."):
        raise CoordinationWorkspaceError(f"{name} must not be '.' or '..'")
    if not _COMPONENT_RE.match(value):
        raise CoordinationWorkspaceError(
            f"{name} must contain only ASCII letters, digits, '.', '_', or '-'"
        )
    return value


def _validate_controller_binding_ref(value: Any, initiative_id: str) -> str:
    if not isinstance(value, str):
        raise CoordinationWorkspaceError(
            "controller_binding_ref must be a string"
        )
    parts = value.split(":")
    if len(parts) != 3:
        raise CoordinationWorkspaceError(
            "controller_binding_ref must have exactly three colon-separated parts"
        )
    if parts[0] != "tracker":
        raise CoordinationWorkspaceError(
            "controller_binding_ref first part must be 'tracker'"
        )
    _validate_component(parts[1], "board_slug")
    ref_initiative = _validate_component(parts[2], "initiative_id")
    if ref_initiative != initiative_id:
        raise CoordinationWorkspaceError(
            "controller_binding_ref initiative part must match supplied initiative_id"
        )
    return value


def _validate_timestamp(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinationWorkspaceError(f"{name} must be an integer")
    if value <= 0:
        raise CoordinationWorkspaceError(f"{name} must be positive")
    return value


def _validate_state(value: Any, name: str) -> str:
    if not isinstance(value, str) or value not in (
        "planned",
        "materializing",
        "materialized",
        "failed",
        "merged",
        "retired",
    ):
        raise CoordinationWorkspaceError(f"{name} must be a valid state")
    return value


def _validate_binding_version(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinationWorkspaceError(f"{name} must be an integer")
    if value <= 0:
        raise CoordinationWorkspaceError(f"{name} must be positive")
    return value


_ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "planned": ("materializing",),
    "materializing": ("materialized", "failed"),
    "failed": ("materializing",),
    "materialized": ("merged",),
    "merged": ("retired",),
}


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


class CoordinationWorkspaceStore:
    """Store for initiative coordination workspaces."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # plan_or_read
    # ------------------------------------------------------------------
    def plan_or_read(
        self,
        initiative_id: str,
        project_id: str,
        repository_identity: str,
        controller_binding_ref: str,
        planned_at: int,
    ) -> Dict[str, Any]:
        initiative_id = _validate_component(initiative_id, "initiative_id")
        project_id = _validate_component(project_id, "project_id")
        repository_identity = _validate_component(repository_identity, "repository_identity")
        controller_binding_ref = _validate_controller_binding_ref(
            controller_binding_ref, initiative_id
        )
        planned_at = _validate_timestamp(planned_at, "planned_at")

        workspace_id = f"coord-{initiative_id}"
        relative_path = f"{initiative_id}/coordination/{repository_identity}"
        branch = f"initiative/{initiative_id}/coordination/{repository_identity}"

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(f"failed to begin transaction: {exc}") from exc

        try:
            # Find the unique initiative card
            card = self._conn.execute(
                "SELECT id FROM adrian_kanban_cards "
                "WHERE initiative_id = ? AND task_id IS NULL",
                (initiative_id,),
            ).fetchone()
            if card is None:
                raise CoordinationWorkspaceError(
                    f"initiative card not found for {initiative_id!r}"
                )
            initiative_card_id: int = card[0]

            # Check for existing workspace
            existing_ws = self._conn.execute(
                "SELECT workspace_id, initiative_card_id, initiative_id, project_id, "
                "lifecycle_state, controller_binding_ref, binding_version, active, "
                "failure_detail, created_at, updated_at "
                "FROM initiative_coordination_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()

            if existing_ws is not None:
                # Verify immutable fields match
                if (
                    existing_ws[1] != initiative_card_id
                    or existing_ws[2] != initiative_id
                    or existing_ws[3] != project_id
                    or existing_ws[5] != controller_binding_ref
                    or existing_ws[7] != 1
                ):
                    raise CoordinationWorkspaceError(
                        "existing workspace field mismatch"
                    )
                _validate_binding_version(
                    existing_ws[6], "existing workspace binding_version"
                )

                # Verify requested member exists and its immutable fields match
                existing_member = self._conn.execute(
                    "SELECT workspace_id, repository_identity, relative_path, branch, "
                    "required_base_sha, observed_head, member_state, failure_detail, observed_at "
                    "FROM initiative_coordination_workspace_members "
                    "WHERE workspace_id = ? AND repository_identity = ?",
                    (workspace_id, repository_identity),
                ).fetchone()
                if existing_member is None:
                    raise CoordinationWorkspaceError(
                        "existing workspace has no matching member"
                    )
                if (
                    existing_member[2] != relative_path
                    or existing_member[3] != branch
                ):
                    raise CoordinationWorkspaceError(
                        "existing member field mismatch"
                    )

                self._conn.execute("COMMIT")
                return self._fetch_workspace_with_members(workspace_id)

            # Create the planned active workspace
            self._conn.execute(
                "INSERT INTO initiative_coordination_workspaces ("
                "workspace_id, initiative_card_id, initiative_id, project_id, "
                "lifecycle_state, controller_binding_ref, binding_version, active, "
                "failure_detail, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'planned', ?, 1, 1, NULL, ?, ?)",
                (
                    workspace_id,
                    initiative_card_id,
                    initiative_id,
                    project_id,
                    controller_binding_ref,
                    planned_at,
                    planned_at,
                ),
            )

            # Create the planned primary member
            self._conn.execute(
                "INSERT INTO initiative_coordination_workspace_members ("
                "workspace_id, repository_identity, relative_path, branch, "
                "required_base_sha, observed_head, member_state, failure_detail, observed_at"
                ") VALUES (?, ?, ?, ?, NULL, NULL, 'planned', NULL, ?)",
                (
                    workspace_id,
                    repository_identity,
                    relative_path,
                    branch,
                    planned_at,
                ),
            )

            self._conn.execute("COMMIT")
            return {
                "workspace_id": workspace_id,
                "initiative_card_id": initiative_card_id,
                "initiative_id": initiative_id,
                "project_id": project_id,
                "lifecycle_state": "planned",
                "controller_binding_ref": controller_binding_ref,
                "binding_version": 1,
                "active": 1,
                "failure_detail": None,
                "created_at": planned_at,
                "updated_at": planned_at,
                "members": [
                    {
                        "workspace_id": workspace_id,
                        "repository_identity": repository_identity,
                        "relative_path": relative_path,
                        "branch": branch,
                        "required_base_sha": None,
                        "observed_head": None,
                        "member_state": "planned",
                        "failure_detail": None,
                        "observed_at": planned_at,
                    }
                ],
            }
        except CoordinationWorkspaceError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CoordinationWorkspaceError(str(exc)) from exc

    # ------------------------------------------------------------------
    # read_active
    # ------------------------------------------------------------------
    def read_active(self, initiative_id: str) -> Dict[str, Any]:
        initiative_id = _validate_component(initiative_id, "initiative_id")

        card = self._conn.execute(
            "SELECT id FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND task_id IS NULL",
            (initiative_id,),
        ).fetchone()
        if card is None:
            raise CoordinationWorkspaceError(
                f"initiative card not found for {initiative_id!r}"
            )
        initiative_card_id: int = card[0]

        ws = self._conn.execute(
            "SELECT workspace_id, initiative_card_id, initiative_id, project_id, "
            "lifecycle_state, controller_binding_ref, binding_version, active, "
            "failure_detail, created_at, updated_at "
            "FROM initiative_coordination_workspaces "
            "WHERE initiative_card_id = ? AND initiative_id = ? AND active = 1",
            (initiative_card_id, initiative_id),
        ).fetchone()
        if ws is None:
            raise CoordinationWorkspaceError(
                f"no active coordination workspace for {initiative_id!r}"
            )

        members = self._conn.execute(
            "SELECT workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, failure_detail, observed_at "
            "FROM initiative_coordination_workspace_members "
            "WHERE workspace_id = ? "
            "ORDER BY repository_identity",
            (ws[0],),
        ).fetchall()

        return {
            "workspace_id": ws[0],
            "initiative_card_id": ws[1],
            "initiative_id": ws[2],
            "project_id": ws[3],
            "lifecycle_state": ws[4],
            "controller_binding_ref": ws[5],
            "binding_version": ws[6],
            "active": ws[7],
            "failure_detail": ws[8],
            "created_at": ws[9],
            "updated_at": ws[10],
            "members": [
                {
                    "workspace_id": m[0],
                    "repository_identity": m[1],
                    "relative_path": m[2],
                    "branch": m[3],
                    "required_base_sha": m[4],
                    "observed_head": m[5],
                    "member_state": m[6],
                    "failure_detail": m[7],
                    "observed_at": m[8],
                }
                for m in members
            ],
        }

    def add_planned_members(
        self,
        workspace_id: str,
        repository_identities: object,
        *,
        expected_binding_version: int,
        at: int,
    ) -> Dict[str, Any]:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(
                f"failed to begin transaction: {exc}"
            ) from exc

        try:
            result = self.add_planned_members_in_active_transaction(
                workspace_id,
                repository_identities,
                expected_binding_version=expected_binding_version,
                at=at,
            )
            self._conn.execute("COMMIT")
            return result
        except CoordinationWorkspaceError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CoordinationWorkspaceError(str(exc)) from exc

    def add_planned_members_in_active_transaction(
        self,
        workspace_id: str,
        repository_identities: object,
        *,
        expected_binding_version: int,
        at: int,
    ) -> Dict[str, Any]:
        if self._conn.in_transaction is not True:
            raise CoordinationWorkspaceError(
                "add_planned_members_in_active_transaction requires an active "
                "transaction"
            )

        workspace_id = _validate_component(workspace_id, "workspace_id")
        expected_binding_version = _validate_binding_version(
            expected_binding_version, "expected_binding_version"
        )
        at = _validate_timestamp(at, "at")

        if not isinstance(repository_identities, (list, tuple)):
            raise CoordinationWorkspaceError(
                "repository_identities must be a list or tuple"
            )
        if not repository_identities:
            raise CoordinationWorkspaceError(
                "repository_identities must not be empty"
            )

        unique_identities: List[str] = []
        for repository_identity in repository_identities:
            if not isinstance(repository_identity, str):
                raise CoordinationWorkspaceError(
                    "repository_identity must be a string"
                )
            _validate_component(repository_identity, "repository_identity")
            if repository_identity in unique_identities:
                raise CoordinationWorkspaceError(
                    "duplicate repository_identity detected"
                )
            unique_identities.append(repository_identity)
        unique_identities.sort()

        workspace = self._conn.execute(
            "SELECT lifecycle_state, binding_version, active, initiative_id "
            "FROM initiative_coordination_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if workspace is None:
            raise CoordinationWorkspaceError(
                f"workspace {workspace_id!r} not found"
            )
        if workspace[2] != 1:
            raise CoordinationWorkspaceError(
                f"workspace {workspace_id!r} is not active"
            )

        current_state = workspace[0]
        if current_state not in ("planned", "materialized"):
            raise CoordinationWorkspaceError(
                f"invalid workspace state: {current_state!r}"
            )
        if workspace[1] != expected_binding_version:
            raise CoordinationWorkspaceError("binding version mismatch")

        initiative_id = workspace[3]
        existing_members = self._conn.execute(
            "SELECT repository_identity "
            "FROM initiative_coordination_workspace_members "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        existing_set = {member[0] for member in existing_members}
        for repository_identity in unique_identities:
            if repository_identity in existing_set:
                raise CoordinationWorkspaceError(
                    f"member {repository_identity!r} already exists"
                )

        next_state = (
            "materializing" if current_state == "materialized" else "planned"
        )
        updated = self._conn.execute(
            "UPDATE initiative_coordination_workspaces "
            "SET lifecycle_state = ?, binding_version = binding_version + 1, "
            "updated_at = ? WHERE workspace_id = ? AND binding_version = ?",
            (next_state, at, workspace_id, expected_binding_version),
        )
        if updated.rowcount != 1:
            raise CoordinationWorkspaceError("guarded workspace update failed")

        for repository_identity in unique_identities:
            relative_path = f"{initiative_id}/coordination/{repository_identity}"
            branch = (
                f"initiative/{initiative_id}/coordination/{repository_identity}"
            )
            self._conn.execute(
                "INSERT INTO initiative_coordination_workspace_members ("
                "workspace_id, repository_identity, relative_path, branch, "
                "required_base_sha, observed_head, member_state, "
                "failure_detail, observed_at"
                ") VALUES (?, ?, ?, ?, NULL, NULL, 'planned', NULL, ?)",
                (
                    workspace_id,
                    repository_identity,
                    relative_path,
                    branch,
                    at,
                ),
            )

        return self._fetch_workspace_with_members(workspace_id)

    def advance_binding_version(
        self,
        workspace_id: str,
        expected_version: int,
        at: int,
    ) -> Dict[str, Any]:
        """Advance a materialized workspace binding version once."""
        workspace_id = _validate_component(workspace_id, "workspace_id")
        expected_version = _validate_binding_version(
            expected_version, "expected_version"
        )
        at = _validate_timestamp(at, "at")

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(
                f"failed to begin transaction: {exc}"
            ) from exc

        try:
            row = self._conn.execute(
                "SELECT lifecycle_state, binding_version, active "
                "FROM initiative_coordination_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise CoordinationWorkspaceError(
                    f"workspace {workspace_id!r} not found"
                )
            if row[2] != 1:
                raise CoordinationWorkspaceError(
                    f"workspace {workspace_id!r} is not active"
                )
            if row[0] != "materialized":
                raise CoordinationWorkspaceError(
                    f"workspace state is {row[0]!r}, expected 'materialized'"
                )
            current_version = _validate_binding_version(
                row[1], "current binding_version"
            )

            if current_version == expected_version:
                updated = self._conn.execute(
                    "UPDATE initiative_coordination_workspaces "
                    "SET binding_version = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND active = 1 "
                    "AND lifecycle_state = 'materialized' AND binding_version = ?",
                    (expected_version + 1, at, workspace_id, expected_version),
                )
                if updated.rowcount != 1:
                    raise CoordinationWorkspaceError(
                        "guarded update failed: rowcount != 1"
                    )
            elif current_version != expected_version + 1:
                raise CoordinationWorkspaceError(
                    "binding version mismatch: "
                    f"current {current_version}, expected {expected_version}"
                )

            self._conn.execute("COMMIT")
            return self._fetch_workspace_with_members(workspace_id)
        except CoordinationWorkspaceError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CoordinationWorkspaceError(str(exc)) from exc

    def _fetch_workspace_with_members(self, workspace_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT workspace_id, initiative_card_id, initiative_id, project_id, "
            "lifecycle_state, controller_binding_ref, binding_version, active, "
            "failure_detail, created_at, updated_at "
            "FROM initiative_coordination_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise CoordinationWorkspaceError(
                f"workspace {workspace_id!r} not found"
            )
        members = self._conn.execute(
            "SELECT workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, failure_detail, "
            "observed_at FROM initiative_coordination_workspace_members "
            "WHERE workspace_id = ? ORDER BY repository_identity",
            (workspace_id,),
        ).fetchall()
        return {
            "workspace_id": row[0],
            "initiative_card_id": row[1],
            "initiative_id": row[2],
            "project_id": row[3],
            "lifecycle_state": row[4],
            "controller_binding_ref": row[5],
            "binding_version": row[6],
            "active": row[7],
            "failure_detail": row[8],
            "created_at": row[9],
            "updated_at": row[10],
            "members": [
                {
                    "workspace_id": member[0],
                    "repository_identity": member[1],
                    "relative_path": member[2],
                    "branch": member[3],
                    "required_base_sha": member[4],
                    "observed_head": member[5],
                    "member_state": member[6],
                    "failure_detail": member[7],
                    "observed_at": member[8],
                }
                for member in members
            ],
        }

    # ------------------------------------------------------------------
    # transition_workspace
    # ------------------------------------------------------------------
    def transition_workspace(
        self,
        workspace_id: str,
        expected_state: str,
        to_state: str,
        at: int,
        failure_detail: Optional[str] = None,
    ) -> Dict[str, Any]:
        workspace_id = _validate_component(workspace_id, "workspace_id")
        expected_state = _validate_state(expected_state, "expected_state")
        to_state = _validate_state(to_state, "to_state")
        at = _validate_timestamp(at, "at")

        if to_state == "failed" and (failure_detail is None or failure_detail.strip() == ""):
            raise CoordinationWorkspaceError("failure_detail is required for failed state")
        if to_state != "failed" and failure_detail is not None and failure_detail.strip() != "":
            raise CoordinationWorkspaceError(
                "failure_detail must be None for non-failure transitions"
            )

        if expected_state != to_state:
            allowed = _ALLOWED_TRANSITIONS.get(expected_state, ())
            if to_state not in allowed:
                raise CoordinationWorkspaceError(
                    f"transition {expected_state!r} -> {to_state!r} not allowed"
                )

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(f"failed to begin transaction: {exc}") from exc

        try:
            ws = self._conn.execute(
                "SELECT workspace_id, initiative_card_id, initiative_id, project_id, "
                "lifecycle_state, controller_binding_ref, binding_version, active, "
                "failure_detail, created_at, updated_at "
                "FROM initiative_coordination_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if ws is None:
                raise CoordinationWorkspaceError(f"workspace {workspace_id!r} not found")

            current_state: str = ws[4]
            if current_state != expected_state:
                raise CoordinationWorkspaceError(
                    f"workspace state is {current_state!r}, expected {expected_state!r}"
                )

            new_active = 0 if to_state == "retired" else 1
            new_failure_detail = failure_detail if to_state == "failed" else None

            if current_state == to_state:
                # Same-state: idempotent commit
                self._conn.execute("COMMIT")
                return {
                    "workspace_id": ws[0],
                    "initiative_card_id": ws[1],
                    "initiative_id": ws[2],
                    "project_id": ws[3],
                    "lifecycle_state": ws[4],
                    "controller_binding_ref": ws[5],
                    "binding_version": ws[6],
                    "active": ws[7],
                    "failure_detail": ws[8],
                    "created_at": ws[9],
                    "updated_at": ws[10],
                }

            cur = self._conn.execute(
                "UPDATE initiative_coordination_workspaces "
                "SET lifecycle_state = ?, active = ?, failure_detail = ?, updated_at = ? "
                "WHERE workspace_id = ? AND lifecycle_state = ?",
                (to_state, new_active, new_failure_detail, at, workspace_id, expected_state),
            )
            if cur.rowcount != 1:
                raise CoordinationWorkspaceError("guarded update failed: rowcount != 1")

            self._conn.execute("COMMIT")
            return {
                "workspace_id": ws[0],
                "initiative_card_id": ws[1],
                "initiative_id": ws[2],
                "project_id": ws[3],
                "lifecycle_state": to_state,
                "controller_binding_ref": ws[5],
                "binding_version": ws[6],
                "active": new_active,
                "failure_detail": new_failure_detail,
                "created_at": ws[9],
                "updated_at": at,
            }
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # ------------------------------------------------------------------
    # transition_member
    # ------------------------------------------------------------------
    def transition_member(
        self,
        workspace_id: str,
        repository_identity: str,
        expected_state: str,
        to_state: str,
        at: int,
        failure_detail: Optional[str] = None,
    ) -> Dict[str, Any]:
        workspace_id = _validate_component(workspace_id, "workspace_id")
        repository_identity = _validate_component(repository_identity, "repository_identity")
        expected_state = _validate_state(expected_state, "expected_state")
        to_state = _validate_state(to_state, "to_state")
        at = _validate_timestamp(at, "at")

        if to_state == "failed" and (failure_detail is None or failure_detail.strip() == ""):
            raise CoordinationWorkspaceError("failure_detail is required for failed state")
        if to_state != "failed" and failure_detail is not None and failure_detail.strip() != "":
            raise CoordinationWorkspaceError(
                "failure_detail must be None for non-failure transitions"
            )

        if expected_state != to_state:
            allowed = _ALLOWED_TRANSITIONS.get(expected_state, ())
            if to_state not in allowed:
                raise CoordinationWorkspaceError(
                    f"transition {expected_state!r} -> {to_state!r} not allowed"
                )

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(f"failed to begin transaction: {exc}") from exc

        try:
            member = self._conn.execute(
                "SELECT workspace_id, repository_identity, relative_path, branch, "
                "required_base_sha, observed_head, member_state, failure_detail, observed_at "
                "FROM initiative_coordination_workspace_members "
                "WHERE workspace_id = ? AND repository_identity = ?",
                (workspace_id, repository_identity),
            ).fetchone()
            if member is None:
                raise CoordinationWorkspaceError(
                    f"member {repository_identity!r} not found in workspace {workspace_id!r}"
                )

            current_state: str = member[6]
            if current_state != expected_state:
                raise CoordinationWorkspaceError(
                    f"member state is {current_state!r}, expected {expected_state!r}"
                )

            new_failure_detail = failure_detail if to_state == "failed" else None

            if current_state == to_state:
                self._conn.execute("COMMIT")
                return {
                    "workspace_id": member[0],
                    "repository_identity": member[1],
                    "relative_path": member[2],
                    "branch": member[3],
                    "required_base_sha": member[4],
                    "observed_head": member[5],
                    "member_state": member[6],
                    "failure_detail": member[7],
                    "observed_at": member[8],
                }

            cur = self._conn.execute(
                "UPDATE initiative_coordination_workspace_members "
                "SET member_state = ?, failure_detail = ?, observed_at = ? "
                "WHERE workspace_id = ? AND repository_identity = ? AND member_state = ?",
                (
                    to_state,
                    new_failure_detail,
                    at,
                    workspace_id,
                    repository_identity,
                    expected_state,
                ),
            )
            if cur.rowcount != 1:
                raise CoordinationWorkspaceError("guarded update failed: rowcount != 1")

            self._conn.execute("COMMIT")
            return {
                "workspace_id": member[0],
                "repository_identity": member[1],
                "relative_path": member[2],
                "branch": member[3],
                "required_base_sha": member[4],
                "observed_head": member[5],
                "member_state": to_state,
                "failure_detail": new_failure_detail,
                "observed_at": at,
            }
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # ------------------------------------------------------------------
    # pin_member_base
    # ------------------------------------------------------------------
    def pin_member_base(
        self,
        workspace_id: str,
        repository_identity: str,
        required_base_sha: str,
        at: int,
    ) -> Dict[str, Any]:
        workspace_id = _validate_component(workspace_id, "workspace_id")
        repository_identity = _validate_component(repository_identity, "repository_identity")
        at = _validate_timestamp(at, "at")
        if not isinstance(required_base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", required_base_sha):
            raise CoordinationWorkspaceError("required_base_sha must be 40 or 64 ASCII hex characters")

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(f"failed to begin transaction: {exc}") from exc

        try:
            ws = self._conn.execute(
                "SELECT active FROM initiative_coordination_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if ws is None:
                raise CoordinationWorkspaceError(f"workspace {workspace_id!r} not found")
            if ws[0] != 1:
                raise CoordinationWorkspaceError(f"workspace {workspace_id!r} is not active")

            member = self._conn.execute(
                "SELECT workspace_id, repository_identity, relative_path, branch, "
                "required_base_sha, observed_head, member_state, failure_detail, observed_at "
                "FROM initiative_coordination_workspace_members "
                "WHERE workspace_id = ? AND repository_identity = ?",
                (workspace_id, repository_identity),
            ).fetchone()
            if member is None:
                raise CoordinationWorkspaceError(
                    f"member {repository_identity!r} not found in workspace {workspace_id!r}"
                )

            current_state: str = member[6]
            if current_state != "planned":
                raise CoordinationWorkspaceError(
                    f"member state is {current_state!r}, expected 'planned'"
                )
            if member[5] is not None:
                raise CoordinationWorkspaceError("member observed_head must be NULL")

            current_base: Optional[str] = member[4]
            if current_base is None:
                cur = self._conn.execute(
                    "UPDATE initiative_coordination_workspace_members "
                    "SET required_base_sha = ?, observed_at = ? "
                    "WHERE workspace_id = ? AND repository_identity = ? "
                    "AND member_state = 'planned' AND observed_head IS NULL AND required_base_sha IS NULL",
                    (required_base_sha, at, workspace_id, repository_identity),
                )
                if cur.rowcount != 1:
                    raise CoordinationWorkspaceError("guarded update failed: rowcount != 1")
                self._conn.execute("COMMIT")
                return {
                    "workspace_id": member[0],
                    "repository_identity": member[1],
                    "relative_path": member[2],
                    "branch": member[3],
                    "required_base_sha": required_base_sha,
                    "observed_head": None,
                    "member_state": "planned",
                    "failure_detail": None,
                    "observed_at": at,
                }

            if current_base == required_base_sha:
                self._conn.execute("COMMIT")
                return {
                    "workspace_id": member[0],
                    "repository_identity": member[1],
                    "relative_path": member[2],
                    "branch": member[3],
                    "required_base_sha": current_base,
                    "observed_head": None,
                    "member_state": "planned",
                    "failure_detail": None,
                    "observed_at": member[8],
                }

            raise CoordinationWorkspaceError("required_base_sha mismatch")
        except CoordinationWorkspaceError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CoordinationWorkspaceError(str(exc)) from exc

    # ------------------------------------------------------------------
    # consume_materialization
    # ------------------------------------------------------------------
    def consume_materialization(
        self,
        workspace_id: str,
        repository_identity: str,
        observed_head: str,
        at: int,
    ) -> Dict[str, Any]:
        workspace_id = _validate_component(workspace_id, "workspace_id")
        repository_identity = _validate_component(repository_identity, "repository_identity")
        at = _validate_timestamp(at, "at")
        if not isinstance(observed_head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", observed_head):
            raise CoordinationWorkspaceError("observed_head must be 40 or 64 ASCII hex characters")

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(f"failed to begin transaction: {exc}") from exc

        try:
            ws = self._conn.execute(
                "SELECT active FROM initiative_coordination_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if ws is None:
                raise CoordinationWorkspaceError(f"workspace {workspace_id!r} not found")
            if ws[0] != 1:
                raise CoordinationWorkspaceError(f"workspace {workspace_id!r} is not active")

            member = self._conn.execute(
                "SELECT workspace_id, repository_identity, relative_path, branch, "
                "required_base_sha, observed_head, member_state, failure_detail, observed_at "
                "FROM initiative_coordination_workspace_members "
                "WHERE workspace_id = ? AND repository_identity = ?",
                (workspace_id, repository_identity),
            ).fetchone()
            if member is None:
                raise CoordinationWorkspaceError(
                    f"member {repository_identity!r} not found in workspace {workspace_id!r}"
                )

            current_state: str = member[6]
            current_head: Optional[str] = member[5]

            if current_state == "materialized" and current_head == observed_head:
                if member[4] is None:
                    raise CoordinationWorkspaceError(
                        "required_base_sha must be non-NULL"
                    )
                self._conn.execute("COMMIT")
                return {
                    "workspace_id": member[0],
                    "repository_identity": member[1],
                    "relative_path": member[2],
                    "branch": member[3],
                    "required_base_sha": member[4],
                    "observed_head": current_head,
                    "member_state": "materialized",
                    "failure_detail": None,
                    "observed_at": member[8],
                }

            if current_state != "materializing":
                raise CoordinationWorkspaceError(
                    f"member state is {current_state!r}, expected 'materializing'"
                )
            if member[4] is None:
                raise CoordinationWorkspaceError("required_base_sha must be non-NULL")

            cur = self._conn.execute(
                "UPDATE initiative_coordination_workspace_members "
                "SET member_state = 'materialized', observed_head = ?, failure_detail = NULL, observed_at = ? "
                "WHERE workspace_id = ? AND repository_identity = ? AND member_state = 'materializing'",
                (observed_head, at, workspace_id, repository_identity),
            )
            if cur.rowcount != 1:
                raise CoordinationWorkspaceError("guarded update failed: rowcount != 1")

            self._conn.execute("COMMIT")
            return {
                "workspace_id": member[0],
                "repository_identity": member[1],
                "relative_path": member[2],
                "branch": member[3],
                "required_base_sha": member[4],
                "observed_head": observed_head,
                "member_state": "materialized",
                "failure_detail": None,
                "observed_at": at,
            }
        except CoordinationWorkspaceError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CoordinationWorkspaceError(str(exc)) from exc

    def replace_member_freshness(
        self,
        workspace_id: str,
        repository_identity: str,
        *,
        expected_observed_head: str,
        expected_required_base_sha: str,
        new_observed_head: str,
        new_required_base_sha: str,
        at: int,
    ) -> Dict[str, Any]:
        workspace_id = _validate_component(workspace_id, "workspace_id")
        repository_identity = _validate_component(
            repository_identity, "repository_identity"
        )
        at = _validate_timestamp(at, "at")

        for name, value in (
            ("expected_observed_head", expected_observed_head),
            ("expected_required_base_sha", expected_required_base_sha),
            ("new_observed_head", new_observed_head),
            ("new_required_base_sha", new_required_base_sha),
        ):
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
            ):
                raise CoordinationWorkspaceError(
                    f"{name} must be 40 or 64 ASCII hex characters"
                )

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(
                f"failed to begin transaction: {exc}"
            ) from exc

        try:
            workspace = self._conn.execute(
                "SELECT active, lifecycle_state "
                "FROM initiative_coordination_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise CoordinationWorkspaceError(
                    f"workspace {workspace_id!r} not found"
                )
            if workspace[0] != 1:
                raise CoordinationWorkspaceError(
                    f"workspace {workspace_id!r} is not active"
                )
            if workspace[1] != "materialized":
                raise CoordinationWorkspaceError(
                    "workspace lifecycle_state is "
                    f"{workspace[1]!r}, expected 'materialized'"
                )

            member = self._conn.execute(
                "SELECT workspace_id, repository_identity, relative_path, branch, "
                "required_base_sha, observed_head, member_state, failure_detail, "
                "observed_at FROM initiative_coordination_workspace_members "
                "WHERE workspace_id = ? AND repository_identity = ?",
                (workspace_id, repository_identity),
            ).fetchone()
            if member is None:
                raise CoordinationWorkspaceError(
                    f"member {repository_identity!r} not found in workspace "
                    f"{workspace_id!r}"
                )

            current_state: str = member[6]
            if current_state != "materialized":
                raise CoordinationWorkspaceError(
                    f"member state is {current_state!r}, expected 'materialized'"
                )

            current_head: Optional[str] = member[5]
            current_base: Optional[str] = member[4]
            if current_head is None or current_base is None:
                raise CoordinationWorkspaceError(
                    "member observed_head and required_base_sha must be non-NULL"
                )

            if (
                current_head == new_observed_head
                and current_base == new_required_base_sha
            ):
                self._conn.execute("COMMIT")
                return {
                    "workspace_id": member[0],
                    "repository_identity": member[1],
                    "relative_path": member[2],
                    "branch": member[3],
                    "required_base_sha": current_base,
                    "observed_head": current_head,
                    "member_state": current_state,
                    "failure_detail": member[7],
                    "observed_at": member[8],
                }

            if (
                current_head != expected_observed_head
                or current_base != expected_required_base_sha
            ):
                raise CoordinationWorkspaceError(
                    "current head/base do not match expected values"
                )

            cursor = self._conn.execute(
                "UPDATE initiative_coordination_workspace_members SET "
                "observed_head = ?, required_base_sha = ?, failure_detail = NULL, "
                "observed_at = ? WHERE workspace_id = ? "
                "AND repository_identity = ? AND observed_head = ? "
                "AND required_base_sha = ? AND member_state = 'materialized'",
                (
                    new_observed_head,
                    new_required_base_sha,
                    at,
                    workspace_id,
                    repository_identity,
                    expected_observed_head,
                    expected_required_base_sha,
                ),
            )
            if cursor.rowcount != 1:
                raise CoordinationWorkspaceError(
                    "guarded update failed: rowcount != 1"
                )

            self._conn.execute("COMMIT")
            return {
                "workspace_id": member[0],
                "repository_identity": member[1],
                "relative_path": member[2],
                "branch": member[3],
                "required_base_sha": new_required_base_sha,
                "observed_head": new_observed_head,
                "member_state": "materialized",
                "failure_detail": None,
                "observed_at": at,
            }
        except CoordinationWorkspaceError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise CoordinationWorkspaceError(str(exc)) from exc

    # ------------------------------------------------------------------
    # append_journal
    # ------------------------------------------------------------------
    def append_journal(self, event_mapping: Dict[str, Any]) -> Dict[str, Any]:
        required_keys = (
            "operation_id",
            "idempotency_id",
            "member_target",
            "ordinal",
            "operation_kind",
            "workspace_id",
            "repository_identity",
            "state",
            "created_at",
        )
        for key in required_keys:
            if key not in event_mapping:
                raise CoordinationWorkspaceError(f"missing required key {key!r}")

        operation_id = _validate_component(event_mapping["operation_id"], "operation_id")
        idempotency_id = _validate_component(event_mapping["idempotency_id"], "idempotency_id")
        member_target = _validate_component(event_mapping["member_target"], "member_target")
        ordinal = _validate_timestamp(event_mapping["ordinal"], "ordinal")
        operation_kind = _validate_component(event_mapping["operation_kind"], "operation_kind")
        workspace_id = _validate_component(event_mapping["workspace_id"], "workspace_id")
        repository_identity = _validate_component(
            event_mapping["repository_identity"], "repository_identity"
        )
        state = event_mapping["state"]
        if not isinstance(state, str) or state not in ("prepared", "verified", "failed"):
            raise CoordinationWorkspaceError("state must be 'prepared', 'verified', or 'failed'")
        created_at = _validate_timestamp(event_mapping["created_at"], "created_at")

        # Canonicalize nullable evidence fields as strings-or-None
        def _canon_evidence(val: Any) -> Optional[str]:
            if val is None:
                return None
            if not isinstance(val, str):
                raise CoordinationWorkspaceError("evidence fields must be strings or None")
            return val

        intended_git_evidence = _canon_evidence(event_mapping.get("intended_git_evidence"))
        intended_filesystem_evidence = _canon_evidence(
            event_mapping.get("intended_filesystem_evidence")
        )
        observed_git_evidence = _canon_evidence(event_mapping.get("observed_git_evidence"))
        observed_filesystem_evidence = _canon_evidence(
            event_mapping.get("observed_filesystem_evidence")
        )
        error_disposition = _canon_evidence(event_mapping.get("error_disposition"))
        recovery_disposition = _canon_evidence(event_mapping.get("recovery_disposition"))
        actor_evidence = _canon_evidence(event_mapping.get("actor_evidence"))

        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise CoordinationWorkspaceError(f"failed to begin transaction: {exc}") from exc

        try:
            # Verify referenced member exists
            member = self._conn.execute(
                "SELECT workspace_id, repository_identity "
                "FROM initiative_coordination_workspace_members "
                "WHERE workspace_id = ? AND repository_identity = ?",
                (workspace_id, repository_identity),
            ).fetchone()
            if member is None:
                raise CoordinationWorkspaceError(
                    f"member {repository_identity!r} not found in workspace {workspace_id!r}"
                )

            # Check for duplicate
            existing = self._conn.execute(
                "SELECT event_id, operation_id, idempotency_id, member_target, ordinal, "
                "operation_kind, workspace_id, repository_identity, state, "
                "intended_git_evidence, intended_filesystem_evidence, "
                "observed_git_evidence, observed_filesystem_evidence, "
                "error_disposition, recovery_disposition, actor_evidence, created_at "
                "FROM initiative_coordination_operation_journal "
                "WHERE operation_id = ? AND member_target = ? AND ordinal = ?",
                (operation_id, member_target, ordinal),
            ).fetchone()

            if existing is not None:
                # Verify payload matches
                expected_payload = (
                    idempotency_id,
                    operation_kind,
                    workspace_id,
                    repository_identity,
                    state,
                    intended_git_evidence,
                    intended_filesystem_evidence,
                    observed_git_evidence,
                    observed_filesystem_evidence,
                    error_disposition,
                    recovery_disposition,
                    actor_evidence,
                    created_at,
                )
                actual_payload = (
                    existing[2],
                    existing[5],
                    existing[6],
                    existing[7],
                    existing[8],
                    existing[9],
                    existing[10],
                    existing[11],
                    existing[12],
                    existing[13],
                    existing[14],
                    existing[15],
                    existing[16],
                )
                if expected_payload != actual_payload:
                    raise CoordinationWorkspaceError(
                        "duplicate journal entry with mismatched payload"
                    )
                self._conn.execute("COMMIT")
                return {
                    "event_id": existing[0],
                    "operation_id": existing[1],
                    "idempotency_id": existing[2],
                    "member_target": existing[3],
                    "ordinal": existing[4],
                    "operation_kind": existing[5],
                    "workspace_id": existing[6],
                    "repository_identity": existing[7],
                    "state": existing[8],
                    "intended_git_evidence": existing[9],
                    "intended_filesystem_evidence": existing[10],
                    "observed_git_evidence": existing[11],
                    "observed_filesystem_evidence": existing[12],
                    "error_disposition": existing[13],
                    "recovery_disposition": existing[14],
                    "actor_evidence": existing[15],
                    "created_at": existing[16],
                }

            cur = self._conn.execute(
                "INSERT INTO initiative_coordination_operation_journal ("
                "operation_id, idempotency_id, member_target, ordinal, "
                "operation_kind, workspace_id, repository_identity, state, "
                "intended_git_evidence, intended_filesystem_evidence, "
                "observed_git_evidence, observed_filesystem_evidence, "
                "error_disposition, recovery_disposition, actor_evidence, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    idempotency_id,
                    member_target,
                    ordinal,
                    operation_kind,
                    workspace_id,
                    repository_identity,
                    state,
                    intended_git_evidence,
                    intended_filesystem_evidence,
                    observed_git_evidence,
                    observed_filesystem_evidence,
                    error_disposition,
                    recovery_disposition,
                    actor_evidence,
                    created_at,
                ),
            )
            event_id: int = cur.lastrowid
            self._conn.execute("COMMIT")
            return {
                "event_id": event_id,
                "operation_id": operation_id,
                "idempotency_id": idempotency_id,
                "member_target": member_target,
                "ordinal": ordinal,
                "operation_kind": operation_kind,
                "workspace_id": workspace_id,
                "repository_identity": repository_identity,
                "state": state,
                "intended_git_evidence": intended_git_evidence,
                "intended_filesystem_evidence": intended_filesystem_evidence,
                "observed_git_evidence": observed_git_evidence,
                "observed_filesystem_evidence": observed_filesystem_evidence,
                "error_disposition": error_disposition,
                "recovery_disposition": recovery_disposition,
                "actor_evidence": actor_evidence,
                "created_at": created_at,
            }
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

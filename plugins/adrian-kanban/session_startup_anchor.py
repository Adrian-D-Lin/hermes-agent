"""Trusted Session Startup anchor resolver."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .coordination_freshness import CoordinationFreshnessController
from .coordination_materialization import CoordinationMaterializer
from .coordination_workspace import CoordinationWorkspaceStore
from .schema import create_schema
from .workspace import _TrustedRepositoryRegistry

__all__ = ["SessionStartupAnchorError", "SessionStartupAnchorResolver"]


class SessionStartupAnchorError(Exception):
    """Fail-closed error for the trusted Session Startup anchor resolver."""


_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _require_dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionStartupAnchorError(f"{name} must be a dict")
    return value


def _require_nonblank_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionStartupAnchorError(f"{name} must be a nonblank string")
    return value


def _require_safe_component(value: Any, name: str) -> str:
    value = _require_nonblank_str(value, name)
    if value in (".", ".."):
        raise SessionStartupAnchorError(f"{name} must not be '.' or '..'")
    if not _COMPONENT_RE.match(value):
        raise SessionStartupAnchorError(
            f"{name} must contain only ASCII letters, digits, '.', '_', or '-'"
        )
    return value


def _canonical_path(path: str) -> str:
    canonical = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if sys.platform == "win32":
        canonical = os.path.normcase(canonical)
    return canonical


def _resolve_repository(
    registry: _TrustedRepositoryRegistry,
    primary_path: str,
) -> str:
    """Resolve project.primary_path to exactly one trusted repository identity."""
    if not os.path.isabs(primary_path):
        raise SessionStartupAnchorError(
            f"project.primary_path must be an absolute path, got {primary_path!r}"
        )
    normalized = _canonical_path(primary_path)
    matches: list[str] = []
    for registration in registry._registrations:
        if not os.path.isabs(registration.repository_root):
            raise SessionStartupAnchorError(
                "repository registration root must be an absolute path, got "
                f"{registration.repository_root!r}"
            )
        if _canonical_path(registration.repository_root) == normalized:
            matches.append(registration.repository_identity)
    if len(matches) == 0:
        raise SessionStartupAnchorError(
            "repository registry: no trusted registration matches "
            f"primary_path {primary_path!r}"
        )
    if len(matches) > 1:
        raise SessionStartupAnchorError(
            "repository registry: ambiguous match for primary_path "
            f"{primary_path!r}: {matches}"
        )
    return matches[0]


def _resolve_positive_epoch(epoch_provider: Optional[Callable[[], int]]) -> int:
    """Resolve a positive integer epoch, reusing the supplied provider if present."""
    epoch = epoch_provider() if epoch_provider is not None else int(time.time())
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise SessionStartupAnchorError(
            f"epoch must be a positive integer, got {epoch!r}"
        )
    return epoch


def _require_positive_int_str(value: Any, name: str) -> int:
    """Require a positive base-10 integer string and return its integer value."""
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise SessionStartupAnchorError(
            f"{name} must be a positive base-10 integer string, got {value!r}"
        )
    return int(value)


def _default_workspace_advancement_confirmer(
    session_id: str,
    advancements: Tuple[Dict[str, Any], ...],
) -> bool:
    expected_keys = {
        "repository_identity",
        "recorded_head",
        "local_head",
        "remote_head",
    }
    if not isinstance(advancements, tuple) or not advancements:
        raise SessionStartupAnchorError(
            "advancements must be a nonempty tuple of exact-key dictionaries"
        )
    for item in advancements:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise SessionStartupAnchorError(
                "advancements must be a nonempty tuple of exact-key dictionaries"
            )
    canonical = json.dumps(
        [dict(item) for item in advancements],
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{session_id}\n{canonical}".encode("utf-8")).hexdigest()

    from tools.approval import request_write_gate_approval

    result = request_write_gate_approval(
        request_id=f"coordination-advancement-{digest}",
        command=canonical,
        description=(
            "Confirm that these clean, non-divergent local commits are expected "
            "before Tracker evidence is updated."
        ),
        session_key=session_id,
        timeout_seconds=300,
    )
    return result.get("approved") is True and result.get("decision") == "once"


class SessionStartupAnchorResolver:
    """Compose existing components into a trusted Session Startup anchor."""

    def __init__(
        self,
        *,
        tracker_database_path: str,
        registry: _TrustedRepositoryRegistry,
        connection_factory: Callable[..., sqlite3.Connection] = sqlite3.connect,
        materializer_factory: Optional[
            Callable[[sqlite3.Connection], CoordinationMaterializer]
        ] = None,
        confirm_trusted_logical_binding: Optional[Callable[..., Any]] = None,
        epoch_provider: Optional[Callable[[], int]] = None,
        active_binding_loader: Optional[Callable[[str], Any]] = None,
        freshness_factory: Optional[
            Callable[
                [
                    sqlite3.Connection,
                    Callable[[Tuple[Dict[str, Any], ...]], bool],
                ],
                CoordinationFreshnessController,
            ]
        ] = None,
        workspace_advancement_confirmer: Optional[
            Callable[[str, Tuple[Dict[str, Any], ...]], bool]
        ] = None,
    ) -> None:
        _require_nonblank_str(tracker_database_path, "tracker_database_path")
        if not os.path.isabs(tracker_database_path):
            raise SessionStartupAnchorError(
                "tracker_database_path must be an absolute path, got "
                f"{tracker_database_path!r}"
            )
        if not isinstance(registry, _TrustedRepositoryRegistry):
            raise SessionStartupAnchorError(
                "registry must be _TrustedRepositoryRegistry"
            )
        if not callable(connection_factory):
            raise SessionStartupAnchorError("connection_factory must be callable")
        if materializer_factory is not None and not callable(materializer_factory):
            raise SessionStartupAnchorError("materializer_factory must be callable")
        if confirm_trusted_logical_binding is not None and not callable(
            confirm_trusted_logical_binding
        ):
            raise SessionStartupAnchorError(
                "confirm_trusted_logical_binding must be callable"
            )
        if epoch_provider is not None and not callable(epoch_provider):
            raise SessionStartupAnchorError("epoch_provider must be callable")
        if active_binding_loader is not None and not callable(active_binding_loader):
            raise SessionStartupAnchorError("active_binding_loader must be callable")
        if freshness_factory is not None and not callable(freshness_factory):
            raise SessionStartupAnchorError("freshness_factory must be callable")
        if workspace_advancement_confirmer is not None and not callable(
            workspace_advancement_confirmer
        ):
            raise SessionStartupAnchorError(
                "workspace_advancement_confirmer must be callable"
            )

        self._tracker_database_path = tracker_database_path
        self._registry = registry
        self._connection_factory = connection_factory
        self._materializer_factory = materializer_factory
        self._confirm_trusted_logical_binding = confirm_trusted_logical_binding
        self._epoch_provider = epoch_provider
        self._active_binding_loader = active_binding_loader
        self._freshness_factory = freshness_factory
        self._workspace_advancement_confirmer = workspace_advancement_confirmer

    def resolve(
        self,
        project: Dict[str, Any],
        initiative: Dict[str, Any],
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve and return the five trusted controller anchor fields."""
        project = _require_dict(project, "project")
        initiative = _require_dict(initiative, "initiative")
        record = _require_dict(record, "record")

        project_id = _require_nonblank_str(project.get("id"), "project.id")
        board_slug = _require_safe_component(
            project.get("board_slug"), "project.board_slug"
        )
        primary_path = _require_nonblank_str(
            project.get("primary_path"), "project.primary_path"
        )
        initiative_id = _require_safe_component(
            initiative.get("initiative_id"), "initiative.initiative_id"
        )
        current_phase = _require_nonblank_str(
            initiative.get("current_phase"), "initiative.current_phase"
        )
        session_id = _require_nonblank_str(
            record.get("session_id"), "record.session_id"
        )

        accepted_segment_id = initiative.get("current_segment_id")
        if accepted_segment_id is not None:
            _require_nonblank_str(
                accepted_segment_id, "initiative.current_segment_id"
            )

        repository_identity = _resolve_repository(self._registry, primary_path)
        controller_binding_ref = f"tracker:{board_slug}:{initiative_id}"

        epoch = _resolve_positive_epoch(self._epoch_provider)

        conn = self._connection_factory(self._tracker_database_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            create_schema(conn)
            conn.commit()

            store = CoordinationWorkspaceStore(conn)
            workspace = store.plan_or_read(
                initiative_id=initiative_id,
                project_id=project_id,
                repository_identity=repository_identity,
                controller_binding_ref=controller_binding_ref,
                planned_at=epoch,
            )
            materializer = self._get_materializer(conn)
            if workspace.get("lifecycle_state") == "materialized":
                self._reconcile_freshness(conn, initiative_id, session_id, epoch)
                materialized = materializer.materialize(
                    initiative_id=initiative_id,
                    actor_evidence=f"session-startup:{session_id}",
                    at=epoch,
                )
            else:
                materializer.materialize(
                    initiative_id=initiative_id,
                    actor_evidence=f"session-startup:{session_id}",
                    at=epoch,
                )
                materialized = self._reconcile_freshness(
                    conn, initiative_id, session_id, epoch
                )
            if not isinstance(materialized, dict):
                raise SessionStartupAnchorError(
                    "materializer.materialize must return a dict"
                )

            member_roots: Tuple[str, ...] = tuple(
                materialized.get("member_roots") or ()
            )
            if not member_roots:
                raise SessionStartupAnchorError(
                    "materialized member_roots must be nonempty"
                )
            for root in member_roots:
                _require_nonblank_str(root, "materialized member root")

            workspace_id = _require_nonblank_str(
                materialized.get("workspace_id"), "materialized workspace_id"
            )
            binding_version = materialized.get("binding_version")
            if (
                isinstance(binding_version, bool)
                or not isinstance(binding_version, int)
                or binding_version <= 0
            ):
                raise SessionStartupAnchorError(
                    "binding_version must be a positive integer, got "
                    f"{binding_version!r}"
                )

            confirm_fn = self._confirm_trusted_logical_binding
            if confirm_fn is None:
                from writegate.tool import confirm_trusted_logical_binding as confirm_fn

            binding_record = confirm_fn(
                session_id=session_id,
                project=project_id,
                initiative=initiative_id,
                board=board_slug,
                logical_workspace_id=workspace_id,
                member_roots=member_roots,
                binding_version=binding_version,
            )
            if binding_record is None:
                raise SessionStartupAnchorError(
                    "WriteGate: confirm_trusted_logical_binding returned None "
                    "(approval declined); no anchor persisted"
                )

            raw_binding_id = getattr(binding_record, "id", None)
            if (
                isinstance(raw_binding_id, bool)
                or not isinstance(raw_binding_id, int)
                or raw_binding_id <= 0
            ):
                raise SessionStartupAnchorError(
                    "binding_record.id must be a positive integer, got "
                    f"{raw_binding_id!r}"
                )
            binding_id = str(raw_binding_id)
            return {
                "accepted_phase": current_phase,
                "accepted_segment_id": accepted_segment_id,
                "logical_workspace_id": workspace_id,
                "writegate_binding_version": str(binding_version),
                "writegate_binding_ref": binding_id,
            }
        finally:
            conn.close()

    def replace(
        self,
        project: Dict[str, Any],
        initiative: Dict[str, Any],
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Perform focused replacement without repeating human selection."""
        project = _require_dict(project, "project")
        initiative = _require_dict(initiative, "initiative")
        record = _require_dict(record, "record")

        project_id = _require_nonblank_str(project.get("id"), "project.id")
        board_slug = _require_safe_component(
            project.get("board_slug"), "project.board_slug"
        )
        primary_path = _require_nonblank_str(
            project.get("primary_path"), "project.primary_path"
        )
        initiative_id = _require_safe_component(
            initiative.get("initiative_id"), "initiative.initiative_id"
        )
        current_phase = _require_nonblank_str(
            initiative.get("current_phase"), "initiative.current_phase"
        )
        session_id = _require_nonblank_str(
            record.get("session_id"), "record.session_id"
        )

        current_segment = initiative.get("current_segment_id")
        if current_segment is not None:
            _require_nonblank_str(
                current_segment, "initiative.current_segment_id"
            )
        stored_phase = _require_nonblank_str(
            record.get("accepted_phase"), "record.accepted_phase"
        )
        stored_segment = record.get("accepted_segment_id")
        if stored_segment is not None:
            _require_nonblank_str(stored_segment, "record.accepted_segment_id")
        stored_workspace_id = _require_nonblank_str(
            record.get("logical_workspace_id"), "record.logical_workspace_id"
        )
        stored_binding_version = _require_positive_int_str(
            record.get("writegate_binding_version"),
            "record.writegate_binding_version",
        )
        _require_positive_int_str(
            record.get("writegate_binding_ref"),
            "record.writegate_binding_ref",
        )
        stored_project_id = _require_nonblank_str(
            record.get("selected_project_id"),
            "record.selected_project_id",
        )
        stored_initiative_id = _require_nonblank_str(
            record.get("selected_initiative_id"),
            "record.selected_initiative_id",
        )
        if stored_project_id != project_id:
            raise SessionStartupAnchorError(
                "record.selected_project_id does not match project.id"
            )
        if stored_initiative_id != initiative_id:
            raise SessionStartupAnchorError(
                "record.selected_initiative_id does not match initiative.initiative_id"
            )
        _resolve_repository(self._registry, primary_path)

        if stored_phase != current_phase or stored_segment != current_segment:
            conn = self._connection_factory(self._tracker_database_path)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                try:
                    conn.execute(
                        "SELECT 1 FROM initiative_coordination_workspaces LIMIT 1"
                    )
                except sqlite3.Error as exc:
                    raise SessionStartupAnchorError(
                        f"coordination-store: schema not present: {exc}"
                    ) from exc
                store = CoordinationWorkspaceStore(conn)
                try:
                    workspace = store.read_active(initiative_id)
                except Exception as exc:
                    raise SessionStartupAnchorError(
                        "coordination-store: failed to read active workspace: "
                        f"{exc}"
                    ) from exc
                if not isinstance(workspace, dict):
                    raise SessionStartupAnchorError(
                        "coordination-store: read_active must return a dict"
                    )
                if workspace.get("active") != 1:
                    raise SessionStartupAnchorError("workspace.active must be 1")
                if workspace.get("lifecycle_state") != "materialized":
                    raise SessionStartupAnchorError(
                        "workspace lifecycle_state must be 'materialized'"
                    )
                if workspace.get("workspace_id") != stored_workspace_id:
                    raise SessionStartupAnchorError(
                        "workspace.workspace_id does not match stored "
                        "logical_workspace_id"
                    )
                if workspace.get("project_id") != project_id:
                    raise SessionStartupAnchorError(
                        "workspace.project_id does not match project.id"
                    )
                if workspace.get("initiative_id") != initiative_id:
                    raise SessionStartupAnchorError(
                        "workspace.initiative_id does not match "
                        "initiative.initiative_id"
                    )
                expected_binding_ref = f"tracker:{board_slug}:{initiative_id}"
                if workspace.get("controller_binding_ref") != expected_binding_ref:
                    raise SessionStartupAnchorError(
                        "workspace.controller_binding_ref does not match expected value"
                    )
                workspace_binding_version = workspace.get("binding_version")
                if (
                    isinstance(workspace_binding_version, bool)
                    or not isinstance(workspace_binding_version, int)
                    or workspace_binding_version <= 0
                ):
                    raise SessionStartupAnchorError(
                        "workspace binding_version must be a positive integer, got "
                        f"{workspace_binding_version!r}"
                    )
                if workspace_binding_version not in (
                    stored_binding_version,
                    stored_binding_version + 1,
                ):
                    raise SessionStartupAnchorError(
                        "workspace binding_version must be stored version or "
                        "stored version + 1"
                    )
                epoch = _resolve_positive_epoch(self._epoch_provider)
                try:
                    self._reconcile_freshness(
                        conn, initiative_id, session_id, epoch
                    )
                except Exception as exc:
                    if isinstance(exc, SessionStartupAnchorError):
                        raise
                    raise SessionStartupAnchorError(
                        f"freshness: reconciliation failed: {exc}"
                    ) from exc
                try:
                    store.advance_binding_version(
                        stored_workspace_id,
                        stored_binding_version,
                        epoch,
                    )
                except Exception as exc:
                    raise SessionStartupAnchorError(
                        "coordination-store: failed to advance binding version: "
                        f"{exc}"
                    ) from exc
            finally:
                conn.close()

        return self.resolve(project, initiative, record)

    def revalidate(
        self,
        project: Dict[str, Any],
        initiative: Dict[str, Any],
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Revalidate authority and safely reconcile repository freshness."""
        project = _require_dict(project, "project")
        initiative = _require_dict(initiative, "initiative")
        record = _require_dict(record, "record")

        project_id = _require_nonblank_str(project.get("id"), "project.id")
        board_slug = _require_safe_component(
            project.get("board_slug"), "project.board_slug"
        )
        primary_path = _require_nonblank_str(
            project.get("primary_path"), "project.primary_path"
        )
        initiative_id = _require_safe_component(
            initiative.get("initiative_id"), "initiative.initiative_id"
        )
        current_phase = _require_nonblank_str(
            initiative.get("current_phase"), "initiative.current_phase"
        )
        session_id = _require_nonblank_str(
            record.get("session_id"), "record.session_id"
        )

        accepted_segment_id = initiative.get("current_segment_id")
        if accepted_segment_id is not None:
            _require_nonblank_str(
                accepted_segment_id, "initiative.current_segment_id"
            )

        stored_phase = _require_nonblank_str(
            record.get("accepted_phase"), "record.accepted_phase"
        )
        stored_segment = record.get("accepted_segment_id")
        if stored_segment is not None:
            _require_nonblank_str(stored_segment, "record.accepted_segment_id")
        stored_workspace_id = _require_nonblank_str(
            record.get("logical_workspace_id"), "record.logical_workspace_id"
        )
        stored_binding_version = _require_positive_int_str(
            record.get("writegate_binding_version"),
            "record.writegate_binding_version",
        )
        stored_binding_ref = _require_positive_int_str(
            record.get("writegate_binding_ref"),
            "record.writegate_binding_ref",
        )
        stored_project_id = _require_nonblank_str(
            record.get("selected_project_id"),
            "record.selected_project_id",
        )
        stored_initiative_id = _require_nonblank_str(
            record.get("selected_initiative_id"),
            "record.selected_initiative_id",
        )
        if stored_project_id != project_id:
            raise SessionStartupAnchorError(
                "record.selected_project_id does not match project.id"
            )
        if stored_initiative_id != initiative_id:
            raise SessionStartupAnchorError(
                "record.selected_initiative_id does not match initiative.initiative_id"
            )

        _resolve_repository(self._registry, primary_path)

        conn = self._connection_factory(self._tracker_database_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                conn.execute(
                    "SELECT 1 FROM initiative_coordination_workspaces LIMIT 1"
                )
            except sqlite3.Error as exc:
                raise SessionStartupAnchorError(
                    f"coordination-store: schema not present: {exc}"
                ) from exc

            store = CoordinationWorkspaceStore(conn)
            try:
                workspace = store.read_active(initiative_id)
            except Exception as exc:
                raise SessionStartupAnchorError(
                    f"coordination-store: failed to read active workspace: {exc}"
                ) from exc
            if not isinstance(workspace, dict):
                raise SessionStartupAnchorError(
                    "coordination-store: read_active must return a dict"
                )
            if workspace.get("active") != 1:
                raise SessionStartupAnchorError("workspace.active must be 1")

            workspace_state = workspace.get("lifecycle_state")
            workspace_id = workspace.get("workspace_id")
            workspace_project_id = workspace.get("project_id")
            workspace_initiative_id = workspace.get("initiative_id")
            workspace_binding_version = workspace.get("binding_version")
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                raise SessionStartupAnchorError(
                    "workspace.workspace_id must be a nonblank string"
                )
            if workspace_project_id != project_id:
                raise SessionStartupAnchorError(
                    "workspace.project_id does not match project.id"
                )
            if workspace_initiative_id != initiative_id:
                raise SessionStartupAnchorError(
                    "workspace.initiative_id does not match initiative.initiative_id"
                )
            expected_binding_ref = f"tracker:{board_slug}:{initiative_id}"
            if workspace.get("controller_binding_ref") != expected_binding_ref:
                raise SessionStartupAnchorError(
                    "workspace.controller_binding_ref does not match expected value"
                )
            if (
                isinstance(workspace_binding_version, bool)
                or not isinstance(workspace_binding_version, int)
                or workspace_binding_version <= 0
            ):
                raise SessionStartupAnchorError(
                    "workspace binding_version must be a positive integer, got "
                    f"{workspace_binding_version!r}"
                )
            if workspace_state != "materialized":
                raise SessionStartupAnchorError(
                    f"workspace lifecycle_state is {workspace_state!r}, expected "
                    "'materialized'; revalidation requires a previously "
                    "materialized workspace"
                )

            epoch = _resolve_positive_epoch(self._epoch_provider)
            self._reconcile_freshness(conn, initiative_id, session_id, epoch)
            try:
                materialized = self._get_materializer(conn).materialize(
                    initiative_id=initiative_id,
                    actor_evidence=f"session-startup-revalidate:{session_id}",
                    at=epoch,
                )
            except Exception as exc:
                raise SessionStartupAnchorError(
                    f"materializer: verification failed: {exc}"
                ) from exc
            if not isinstance(materialized, dict):
                raise SessionStartupAnchorError(
                    "materializer.materialize must return a dict"
                )

            member_roots: Tuple[str, ...] = tuple(
                materialized.get("member_roots") or ()
            )
            if not member_roots:
                raise SessionStartupAnchorError(
                    "materialized member_roots must be nonempty"
                )
            for root in member_roots:
                _require_nonblank_str(root, "materialized member root")
            materialized_workspace_id = _require_nonblank_str(
                materialized.get("workspace_id"), "materialized workspace_id"
            )
            if materialized_workspace_id != workspace_id:
                raise SessionStartupAnchorError(
                    "materialized workspace_id does not match workspace.workspace_id"
                )
            materialized_binding_version = materialized.get("binding_version")
            if (
                isinstance(materialized_binding_version, bool)
                or not isinstance(materialized_binding_version, int)
                or materialized_binding_version <= 0
            ):
                raise SessionStartupAnchorError(
                    "materialized binding_version must be a positive integer, got "
                    f"{materialized_binding_version!r}"
                )
            if materialized_binding_version != workspace_binding_version:
                raise SessionStartupAnchorError(
                    "materializer binding_version does not match workspace "
                    "binding_version"
                )

            loader = self._active_binding_loader
            if loader is None:
                from writegate.registry import get_registry

                loader = get_registry().get_active_binding
            try:
                binding = loader(session_id)
            except Exception as exc:
                raise SessionStartupAnchorError(
                    f"binding-loader: failed to load active binding: {exc}"
                ) from exc

            reasons: list[str] = []
            if materialized_workspace_id != stored_workspace_id:
                reasons.append("logical_workspace_changed")
            if workspace_binding_version != stored_binding_version:
                reasons.append("binding_version_changed")

            canonical_member_roots = tuple(
                _canonical_path(root) for root in member_roots
            )
            current_binding_ref: Optional[str]
            if binding is None:
                reasons.append("active_binding_missing")
                current_binding_ref = None
            else:
                raw_binding_id = getattr(binding, "id", None)
                if (
                    isinstance(raw_binding_id, bool)
                    or not isinstance(raw_binding_id, int)
                    or raw_binding_id <= 0
                ):
                    raise SessionStartupAnchorError(
                        "binding.id must be a positive integer, got "
                        f"{raw_binding_id!r}"
                    )
                binding_fields = {
                    "project": getattr(binding, "project", None),
                    "initiative": getattr(binding, "initiative", None),
                    "board": getattr(binding, "board", None),
                    "logical_workspace_id": getattr(
                        binding, "logical_workspace_id", None
                    ),
                }
                for name, value in binding_fields.items():
                    _require_nonblank_str(value, f"binding.{name}")
                binding_roots = getattr(binding, "member_roots", None)
                if not isinstance(binding_roots, (tuple, list)) or not binding_roots:
                    raise SessionStartupAnchorError(
                        "binding.member_roots must be a nonempty tuple or list"
                    )
                for root in binding_roots:
                    _require_nonblank_str(root, "binding member root")
                binding_version = getattr(binding, "binding_version", None)
                if (
                    isinstance(binding_version, bool)
                    or not isinstance(binding_version, int)
                    or binding_version <= 0
                ):
                    raise SessionStartupAnchorError(
                        "binding.binding_version must be a positive integer, got "
                        f"{binding_version!r}"
                    )
                if binding_fields["project"] != project_id:
                    reasons.append("binding_project_changed")
                if binding_fields["initiative"] != initiative_id:
                    reasons.append("binding_initiative_changed")
                if binding_fields["board"] != board_slug:
                    reasons.append("binding_board_changed")
                if binding_fields["logical_workspace_id"] != materialized_workspace_id:
                    reasons.append("binding_workspace_changed")
                canonical_binding_roots = tuple(
                    _canonical_path(root) for root in binding_roots
                )
                if canonical_binding_roots != canonical_member_roots:
                    reasons.append("binding_membership_changed")
                if binding_version != workspace_binding_version:
                    reasons.append("binding_version_stale")
                current_binding_ref = str(raw_binding_id)
                if raw_binding_id != stored_binding_ref:
                    reasons.append("binding_ref_changed")

            if stored_phase != current_phase:
                reasons.append("phase_changed")
            if stored_segment != accepted_segment_id:
                reasons.append("segment_changed")

            return {
                "classification": (
                    "focused_revalidation_required" if reasons else "unchanged"
                ),
                "reasons": reasons,
                "accepted_phase": current_phase,
                "accepted_segment_id": accepted_segment_id,
                "logical_workspace_id": materialized_workspace_id,
                "writegate_binding_version": str(workspace_binding_version),
                "writegate_binding_ref": current_binding_ref,
            }
        finally:
            conn.close()

    def _get_materializer(
        self, conn: sqlite3.Connection
    ) -> CoordinationMaterializer:
        if self._materializer_factory is not None:
            return self._materializer_factory(conn)
        return CoordinationMaterializer(conn=conn, registry=self._registry)

    def _get_freshness(
        self, conn: sqlite3.Connection, session_id: str
    ) -> CoordinationFreshnessController:
        confirmer = self._workspace_advancement_confirmer
        if confirmer is None:
            confirmer = _default_workspace_advancement_confirmer

        def advancement_confirmer(
            advancements: Tuple[Dict[str, Any], ...],
        ) -> bool:
            return confirmer(session_id, advancements)

        if self._freshness_factory is not None:
            return self._freshness_factory(conn, advancement_confirmer)
        return CoordinationFreshnessController(
            conn=conn,
            registry=self._registry,
            advancement_confirmer=advancement_confirmer,
        )

    def _reconcile_freshness(
        self,
        conn: sqlite3.Connection,
        initiative_id: str,
        session_id: str,
        epoch: int,
    ) -> Dict[str, Any]:
        try:
            result = self._get_freshness(conn, session_id).reconcile(
                initiative_id=initiative_id,
                actor_evidence=f"session-startup:{session_id}",
                at=epoch,
            )
        except SessionStartupAnchorError:
            raise
        except Exception as exc:
            raise SessionStartupAnchorError(
                f"freshness: reconciliation failed: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise SessionStartupAnchorError(
                "freshness: reconcile must return a dict"
            )
        return result

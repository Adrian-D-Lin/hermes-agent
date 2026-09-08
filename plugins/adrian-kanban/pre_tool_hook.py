from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from writegate import registry as _writegate

from .segment_manifest import PreparedSegmentManifest, prepare_segment_manifest
from .task_inputs import (
    PreparedManifest,
    TaskInputPreparationContext,
    prepare_task_input_manifest,
)

_GIT_TIMEOUT_SECONDS = 30


class GitTaskInputPreparer:
    def __init__(self, registry_getter: Callable[[], Any] | None = None) -> None:
        self._registry_getter = registry_getter or _writegate.get_registry

    def __call__(
        self,
        payload: dict[str, Any],
        preparation_context: TaskInputPreparationContext,
    ) -> PreparedManifest:
        if type(preparation_context) is not TaskInputPreparationContext:
            raise ValueError(
                "preparation_context must be a TaskInputPreparationContext"
            )
        raw_manifest = payload.get("task_input_manifest_v1")
        if not isinstance(raw_manifest, dict):
            raise ValueError("task_input_manifest_v1 must be a dict")

        def read_git_blob(commit: str, path: str) -> bytes:
            try:
                registry = self._registry_getter()
                binding = registry.get_active_binding(preparation_context.session_id)
            except Exception:
                raise ValueError(
                    "failed to resolve trusted session binding"
                ) from None

            if binding is None:
                raise ValueError("no active binding for session")

            worktree_path = getattr(binding, "worktree_path", None)
            if not isinstance(worktree_path, str) or not worktree_path.strip():
                raise ValueError("binding worktree_path must be a nonblank string")

            resolved = Path(worktree_path).expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError("worktree_path is not a directory")

            root = str(resolved)
            try:
                result = subprocess.run(
                    ["git", "-C", root, "cat-file", "blob", f"{commit}:{path}"],
                    capture_output=True,
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                raise ValueError("git operation timed out") from None
            except FileNotFoundError:
                raise ValueError("git executable not found") from None
            except OSError:
                raise ValueError("git operation failed") from None

            if result.returncode != 0:
                raise ValueError("failed to read git object")

            return result.stdout

        return prepare_task_input_manifest(raw_manifest, read_git_blob)


class GitSegmentManifestPreparer:
    def __init__(
        self,
        trusted_repository_ids_getter: Callable[[], frozenset[str]],
        registry_getter: Callable[[], Any] | None = None,
    ) -> None:
        if not callable(trusted_repository_ids_getter):
            raise TypeError("trusted_repository_ids_getter must be callable")
        if registry_getter is not None and not callable(registry_getter):
            raise TypeError("registry_getter must be callable or None")
        self._trusted_repository_ids_getter = trusted_repository_ids_getter
        self._registry_getter = registry_getter or _writegate.get_registry

    def __call__(
        self,
        payload: dict[str, Any],
        preparation_context: TaskInputPreparationContext,
    ) -> PreparedSegmentManifest:
        if type(preparation_context) is not TaskInputPreparationContext:
            raise ValueError(
                "preparation_context must be a TaskInputPreparationContext"
            )
        initiative_id = payload.get("initiative_id")
        if not isinstance(initiative_id, str) or not initiative_id.strip():
            raise ValueError("initiative_id must be a nonblank string")
        if payload.get("update_kind") != "segment_manifest_projection":
            raise ValueError("update_kind must be segment_manifest_projection")
        update = payload.get("update")
        if not isinstance(update, dict):
            raise ValueError("update must be a dict")

        def read_git_blob(commit: str, path: str) -> bytes:
            try:
                registry = self._registry_getter()
                binding = registry.get_active_binding(preparation_context.session_id)
            except Exception:
                raise ValueError(
                    "failed to resolve trusted session binding"
                ) from None
            if binding is None:
                raise ValueError("no active binding for session")
            worktree_path = getattr(binding, "worktree_path", None)
            if not isinstance(worktree_path, str) or not worktree_path.strip():
                raise ValueError("binding worktree_path must be a nonblank string")
            resolved = Path(worktree_path).expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError("worktree_path is not a directory")
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(resolved),
                        "cat-file",
                        "blob",
                        f"{commit}:{path}",
                    ],
                    capture_output=True,
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                raise ValueError("git operation timed out") from None
            except FileNotFoundError:
                raise ValueError("git executable not found") from None
            except OSError:
                raise ValueError("git operation failed") from None
            if result.returncode != 0:
                raise ValueError("failed to read git object")
            return result.stdout

        return prepare_segment_manifest(
            update,
            read_git_blob,
            self._trusted_repository_ids_getter(),
            expected_initiative_id=initiative_id,
        )


def _sanitize_path(path: str) -> str:
    if not path:
        return "<unknown>"
    sanitized = "".join(
        character
        for character in path
        if character.isprintable() and character not in "\n\r\t"
    )
    if len(sanitized) > 100:
        sanitized = sanitized[:100] + "..."
    return sanitized if sanitized else "<unknown>"


def build_pre_tool_hook(
    preparer: Callable[
        [dict[str, Any], TaskInputPreparationContext], PreparedManifest
    ],
    segment_preparer: Callable[
        [dict[str, Any], TaskInputPreparationContext], PreparedSegmentManifest
    ]
    | None = None,
):
    def hook(
        tool_name: str = "",
        args: object = None,
        **runtime_fields: object,
    ) -> dict[str, str] | None:
        if tool_name not in ("kanban_create", "kanban_update_initiative"):
            return None

        if not isinstance(args, dict):
            return {
                "action": "block",
                "message": "declared_inputs_accessible = no; invalid arguments",
            }

        if tool_name == "kanban_update_initiative":
            if args.get("update_kind") != "segment_manifest_projection":
                return None
            if segment_preparer is None:
                return {
                    "action": "block",
                    "message": "segment_manifest_valid = no; preparer not configured",
                }

        if (
            "lifecycle_contract_v1" in args
            and "task_input_manifest_v1" not in args
        ):
            return {
                "action": "block",
                "message": "lifecycle create requires task_input_manifest_v1",
            }

        if tool_name == "kanban_create" and "task_input_manifest_v1" not in args:
            return None

        session_id = runtime_fields.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return {
                "action": "block",
                "message": "declared_inputs_accessible = no; missing session_id",
            }

        try:
            context = TaskInputPreparationContext(
                session_id=session_id.strip(),
                execution_context="model-tool-preflight",
            )
            if tool_name == "kanban_update_initiative":
                segment_preparer(args, context)
            else:
                preparer(args, context)
            return None
        except ValueError as exc:
            reason = str(exc)
        except Exception:
            reason = "verification failed"

        if tool_name == "kanban_update_initiative":
            update = args.get("update")
            safe_path = ""
            if isinstance(update, dict):
                path = update.get("manifest_path")
                if isinstance(path, str) and path.strip():
                    safe_path = _sanitize_path(path.strip())
            if safe_path:
                return {
                    "action": "block",
                    "message": (
                        "segment_manifest_valid = no; "
                        f"path {safe_path}: {reason}"
                    ),
                }
            return {
                "action": "block",
                "message": f"segment_manifest_valid = no; {reason}",
            }

        manifest = args.get("task_input_manifest_v1")
        safe_path = ""
        if isinstance(manifest, dict):
            entries = manifest.get("entries")
            if isinstance(entries, list) and entries:
                first = entries[0]
                if isinstance(first, dict):
                    path = first.get("workspace_path")
                    if isinstance(path, str) and path.strip():
                        safe_path = _sanitize_path(path.strip())

        if safe_path:
            return {
                "action": "block",
                "message": (
                    "declared_inputs_accessible = no; "
                    f"path {safe_path}: {reason}"
                ),
            }
        return {
            "action": "block",
            "message": f"declared_inputs_accessible = no; {reason}",
        }

    return hook


__all__ = [
    "GitSegmentManifestPreparer",
    "GitTaskInputPreparer",
    "build_pre_tool_hook",
]

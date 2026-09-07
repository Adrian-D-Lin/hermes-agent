from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from writegate import registry as _writegate

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
):
    def hook(
        tool_name: str = "",
        args: object = None,
        **runtime_fields: object,
    ) -> dict[str, str] | None:
        if tool_name != "kanban_create":
            return None

        if not isinstance(args, dict):
            return {
                "action": "block",
                "message": "declared_inputs_accessible = no; invalid arguments",
            }

        if (
            "lifecycle_contract_v1" in args
            and "task_input_manifest_v1" not in args
        ):
            return {
                "action": "block",
                "message": "lifecycle create requires task_input_manifest_v1",
            }

        if "task_input_manifest_v1" not in args:
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
            preparer(args, context)
            return None
        except ValueError as exc:
            reason = str(exc)
        except Exception:
            reason = "verification failed"

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
    "GitTaskInputPreparer",
    "build_pre_tool_hook",
]

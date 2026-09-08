"""Phase result preparation for the Adrian Kanban plugin."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from .phase_d1 import prepare_d1_result
from .phase_d2 import prepare_d2_result
from .task_inputs import TaskInputPreparationContext
from .published_git import read_published_blob, _git
from writegate import registry as _writegate


class GitPhaseResultPreparer:
    def __init__(self, registry_getter: Optional[Callable[[], Any]] = None) -> None:
        if registry_getter is not None and not callable(registry_getter):
            raise TypeError("registry_getter must be callable or None")
        self._registry_getter = registry_getter or _writegate.get_registry

    def __call__(
        self, payload: Any, preparation_context: TaskInputPreparationContext
    ) -> Any:
        if type(preparation_context) is not TaskInputPreparationContext:
            raise ValueError(
                "preparation_context must be a TaskInputPreparationContext"
            )
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        if payload.get("update_kind") != "phase_result":
            raise ValueError("update_kind must be phase_result")
        initiative_id = payload.get("initiative_id")
        if not isinstance(initiative_id, str) or not initiative_id.strip():
            raise ValueError("initiative_id must be a nonblank string")
        update = payload.get("update")
        if not isinstance(update, dict):
            raise ValueError("update must be a dict")
        phase = update.get("phase")
        if update.get("result_kind") != "phase_close":
            raise ValueError("result_kind must be phase_close")
        if phase not in ("D1", "D2"):
            raise ValueError("phase must be D1 or D2")
        root = self._resolve_root(preparation_context)
        if phase == "D1":
            return prepare_d1_result(
                initiative_id,
                update,
                lambda commit, path: read_published_blob(root, commit, path),
            )

        def published_reader(commit, path):
            return read_published_blob(root, commit, path)

        def immutable_reader(commit, path):
            type_result = _git(root, "cat-file", "-t", commit)
            if type_result.returncode != 0 or type_result.stdout.strip() != b"commit":
                raise ValueError(
                    "The pinned review is not a commit object; verify the exact pinned "
                    "commit and retry."
                )
            blob = _git(root, "cat-file", "blob", f"{commit}:{path}")
            if blob.returncode != 0:
                raise ValueError(
                    "The pinned review could not be read from the commit; verify the exact "
                    "pinned commit and path, then retry."
                )
            return blob.stdout

        return prepare_d2_result(
            initiative_id, update, published_reader, immutable_reader
        )

    def _resolve_root(self, preparation_context: TaskInputPreparationContext) -> str:
        try:
            registry = self._registry_getter()
            binding = registry.get_active_binding(preparation_context.session_id)
        except Exception:
            raise ValueError("failed to resolve trusted session binding") from None
        if binding is None:
            raise ValueError("no active binding for session")
        worktree_path = getattr(binding, "worktree_path", None)
        if not isinstance(worktree_path, str) or not worktree_path.strip():
            raise ValueError("binding worktree_path must be a nonblank string")
        resolved = Path(worktree_path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("worktree_path is not a directory")
        return str(resolved)

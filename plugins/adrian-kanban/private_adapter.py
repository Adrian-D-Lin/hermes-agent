import contextlib
import os
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional, Any

from hermes_cli import kanban_db as _kb
from .capability import CapabilityBinding
from .provider import AdrianKanbanAuthorityProvider, _capability_scope

__all__ = ()


class _PrivateAdapterRejected(RuntimeError):
    pass


def _is_exact_str(value: Any) -> bool:
    return type(value) is str


def _is_nonblank_str(value: Any) -> bool:
    return _is_exact_str(value) and value.strip() != ""


def _is_optional_str(value: Any) -> bool:
    return value is None or _is_exact_str(value)


def _is_optional_nonblank_str(value: Any) -> bool:
    return value is None or _is_nonblank_str(value)


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool) and value > 0


def _is_non_negative_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool) and value >= 0


def _is_optional_positive_int(value: Any) -> bool:
    return value is None or _is_positive_int(value)


def _is_exact_dict(value: Any) -> bool:
    return type(value) is dict


def _is_optional_dict(value: Any) -> bool:
    return value is None or _is_exact_dict(value)


def _is_exact_bool(value: Any) -> bool:
    return type(value) is bool


def _is_tuple_of_unique_nonblank_strs(value: Any) -> bool:
    if type(value) is not tuple:
        return False
    seen = set()
    for item in value:
        if not _is_nonblank_str(item):
            return False
        if item in seen:
            return False
        seen.add(item)
    return True


def _is_optional_tuple_of_unique_nonblank_strs(value: Any) -> bool:
    return value is None or _is_tuple_of_unique_nonblank_strs(value)


@dataclass(frozen=True)
class _CreateTaskArgs:
    task_id: str
    title: str
    assignee: str
    body: Optional[str] = None
    parents: tuple[str, ...] = ()
    tenant: Optional[str] = None
    priority: int = 0
    workspace_kind: str = "scratch"
    workspace_path: Optional[str] = None
    project: Optional[str] = None
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    board: Optional[str] = None

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_nonblank_str(self.title):
            raise _PrivateAdapterRejected("title must be a nonblank string")
        if not _is_nonblank_str(self.assignee):
            raise _PrivateAdapterRejected("assignee must be a nonblank string")
        if not _is_optional_nonblank_str(self.body):
            raise _PrivateAdapterRejected("body must be None or a nonblank string")
        if not _is_tuple_of_unique_nonblank_strs(self.parents):
            raise _PrivateAdapterRejected(
                "parents must be a tuple of unique nonblank strings"
            )
        if not _is_optional_nonblank_str(self.tenant):
            raise _PrivateAdapterRejected("tenant must be None or a nonblank string")
        if not _is_non_negative_int(self.priority):
            raise _PrivateAdapterRejected("priority must be a non-negative integer")
        if not _is_nonblank_str(self.workspace_kind):
            raise _PrivateAdapterRejected("workspace_kind must be a nonblank string")
        if not _is_optional_nonblank_str(self.workspace_path):
            raise _PrivateAdapterRejected(
                "workspace_path must be None or a nonblank string"
            )
        if not _is_optional_nonblank_str(self.project):
            raise _PrivateAdapterRejected("project must be None or a nonblank string")
        if not _is_exact_bool(self.goal_mode):
            raise _PrivateAdapterRejected("goal_mode must be a bool")
        if not _is_optional_positive_int(self.goal_max_turns):
            raise _PrivateAdapterRejected(
                "goal_max_turns must be None or a positive integer"
            )
        if not _is_optional_nonblank_str(self.model):
            raise _PrivateAdapterRejected("model must be None or a nonblank string")
        if not _is_optional_nonblank_str(self.provider):
            raise _PrivateAdapterRejected("provider must be None or a nonblank string")
        if self.provider is not None and self.model is None:
            raise _PrivateAdapterRejected("provider requires a model")
        if not _is_optional_nonblank_str(self.board):
            raise _PrivateAdapterRejected("board must be None or a nonblank string")


@dataclass(frozen=True)
class _CompleteTaskArgs:
    task_id: str
    result: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    created_cards: Optional[tuple[str, ...]] = None
    expected_run_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_optional_str(self.result):
            raise _PrivateAdapterRejected("result must be None or a string")
        if not _is_optional_str(self.summary):
            raise _PrivateAdapterRejected("summary must be None or a string")
        if not _is_optional_dict(self.metadata):
            raise _PrivateAdapterRejected("metadata must be None or a dict")
        if not _is_optional_tuple_of_unique_nonblank_strs(self.created_cards):
            raise _PrivateAdapterRejected(
                "created_cards must be None or a tuple of unique nonblank strings"
            )
        if not _is_optional_positive_int(self.expected_run_id):
            raise _PrivateAdapterRejected(
                "expected_run_id must be None or a positive integer"
            )


@dataclass(frozen=True)
class _BlockTaskArgs:
    task_id: str
    reason: Optional[str] = None
    kind: Optional[str] = None
    expected_run_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_optional_str(self.reason):
            raise _PrivateAdapterRejected("reason must be None or a string")
        if not _is_optional_str(self.kind):
            raise _PrivateAdapterRejected("kind must be None or a string")
        if not _is_optional_positive_int(self.expected_run_id):
            raise _PrivateAdapterRejected(
                "expected_run_id must be None or a positive integer"
            )


@dataclass(frozen=True)
class _UnblockTaskArgs:
    task_id: str

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")


@dataclass(frozen=True)
class _CommentArgs:
    task_id: str
    author: str
    body: str

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_nonblank_str(self.author):
            raise _PrivateAdapterRejected("author must be a nonblank string")
        if not _is_nonblank_str(self.body):
            raise _PrivateAdapterRejected("body must be a nonblank string")


@dataclass(frozen=True)
class _HeartbeatArgs:
    task_id: str
    claim_lock: str
    note: Optional[str] = None
    expected_run_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_nonblank_str(self.claim_lock):
            raise _PrivateAdapterRejected("claim_lock must be a nonblank string")
        if not _is_optional_str(self.note):
            raise _PrivateAdapterRejected("note must be None or a string")
        if not _is_optional_positive_int(self.expected_run_id):
            raise _PrivateAdapterRejected(
                "expected_run_id must be None or a positive integer"
            )


@dataclass(frozen=True)
class _RequestChangesArgs:
    task_id: str
    reason: str
    expected_run_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_nonblank_str(self.reason):
            raise _PrivateAdapterRejected("reason must be a nonblank string")
        if not _is_optional_positive_int(self.expected_run_id):
            raise _PrivateAdapterRejected(
                "expected_run_id must be None or a positive integer"
            )


@dataclass(frozen=True)
class _RequestReviewArgs:
    task_id: str
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    reviewer: Optional[str] = None
    expected_run_id: Optional[int] = None
    force: bool = False
    with_reason: bool = False

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_optional_str(self.summary):
            raise _PrivateAdapterRejected("summary must be None or a string")
        if not _is_optional_dict(self.metadata):
            raise _PrivateAdapterRejected("metadata must be None or a dict")
        if not _is_optional_str(self.reviewer):
            raise _PrivateAdapterRejected("reviewer must be None or a string")
        if not _is_optional_positive_int(self.expected_run_id):
            raise _PrivateAdapterRejected(
                "expected_run_id must be None or a positive integer"
            )
        if not _is_exact_bool(self.force):
            raise _PrivateAdapterRejected("force must be a bool")
        if not _is_exact_bool(self.with_reason):
            raise _PrivateAdapterRejected("with_reason must be a bool")


@dataclass(frozen=True)
class _LinkArgs:
    parent_id: str
    child_id: str

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.parent_id):
            raise _PrivateAdapterRejected("parent_id must be a nonblank string")
        if not _is_nonblank_str(self.child_id):
            raise _PrivateAdapterRejected("child_id must be a nonblank string")


@dataclass(frozen=True)
class _LaunchTaskArgs:
    task_id: str
    expected_assignee: str
    board: Optional[str] = None
    ttl_seconds: Optional[int] = None
    failure_limit: int = _kb.DEFAULT_SPAWN_FAILURE_LIMIT

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_nonblank_str(self.expected_assignee):
            raise _PrivateAdapterRejected("expected_assignee must be a nonblank string")
        if self.board is not None and not (
            _is_exact_str(self.board) and self.board.strip() != ""
        ):
            raise _PrivateAdapterRejected("board must be None or a nonblank string")
        if not _is_optional_positive_int(self.ttl_seconds):
            raise _PrivateAdapterRejected(
                "ttl_seconds must be None or a positive integer"
            )
        if not _is_positive_int(self.failure_limit):
            raise _PrivateAdapterRejected("failure_limit must be a positive integer")


@dataclass(frozen=True)
class _AttachArgs:
    task_id: str
    filename: str
    data: bytes
    content_type: Optional[str] = None
    uploaded_by: str = "adrian-kanban"
    board: Optional[str] = None

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
        if not _is_nonblank_str(self.filename):
            raise _PrivateAdapterRejected("filename must be a nonblank string")
        if type(self.data) is not bytes:
            raise _PrivateAdapterRejected("data must be bytes")
        if not _is_optional_nonblank_str(self.content_type):
            raise _PrivateAdapterRejected(
                "content_type must be None or a nonblank string"
            )
        if not _is_nonblank_str(self.uploaded_by):
            raise _PrivateAdapterRejected("uploaded_by must be a nonblank string")
        if not _is_optional_nonblank_str(self.board):
            raise _PrivateAdapterRejected("board must be None or a nonblank string")


_OPERATION_ARGUMENT_TYPES = MappingProxyType({
    "kanban_create": _CreateTaskArgs,
    "kanban_complete": _CompleteTaskArgs,
    "kanban_block": _BlockTaskArgs,
    "kanban_unblock": _UnblockTaskArgs,
    "kanban_comment": _CommentArgs,
    "kanban_heartbeat": _HeartbeatArgs,
    "kanban_request_changes": _RequestChangesArgs,
    "kanban_request_review": _RequestReviewArgs,
    "kanban_link": _LinkArgs,
    "kanban_launch": _LaunchTaskArgs,
    "kanban_attach": _AttachArgs,
    "kanban_attach_url": _AttachArgs,
})


# Only operations whose native mutators already use nested savepoint
# semantics may be admitted inside the boundary's outer transaction. Any
# other operation is rejected explicitly rather than falsely claimed safe.
_ACTIVE_TRANSACTION_OPERATIONS = frozenset({
    "kanban_create",
    "kanban_complete",
    "kanban_block",
    "kanban_unblock",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_request_changes",
    "kanban_request_review",
    "kanban_link",
    "kanban_attach",
    "kanban_attach_url",
})


class _PrivateNativeAdapter:
    def __init__(
        self,
        provider: AdrianKanbanAuthorityProvider,
        conn: sqlite3.Connection,
        *,
        spawn_fn: Optional[Any] = None,
    ) -> None:
        if type(provider) is not AdrianKanbanAuthorityProvider:
            raise _PrivateAdapterRejected(
                "provider must be an AdrianKanbanAuthorityProvider"
            )
        if type(conn) is not sqlite3.Connection:
            raise _PrivateAdapterRejected("conn must be a sqlite3.Connection")
        if spawn_fn is not None and not callable(spawn_fn):
            raise _PrivateAdapterRejected("spawn_fn must be None or callable")
        self._provider = provider
        self._conn = conn
        self._spawn_fn = spawn_fn
        self._active_capability: Optional[Any] = None
        self._active_binding: Optional[CapabilityBinding] = None
        self._deferred_cleanup_task_ids: list[str] = []
        self._rollback_attachment_paths: list[str] = []

    def _validate_binding_and_arguments(
        self, capability: Any, binding: CapabilityBinding, arguments: Any
    ) -> str:
        if type(binding) is not CapabilityBinding:
            raise _PrivateAdapterRejected("binding must be a CapabilityBinding")

        operation = binding.operation
        if operation not in _OPERATION_ARGUMENT_TYPES:
            raise _PrivateAdapterRejected(f"unknown operation: {operation}")

        expected_args_type = _OPERATION_ARGUMENT_TYPES[operation]
        if type(arguments) is not expected_args_type:
            raise _PrivateAdapterRejected(
                f"arguments must be {expected_args_type.__name__}"
            )

        if operation == "kanban_link":
            if binding.target != arguments.child_id:
                raise _PrivateAdapterRejected(
                    "binding.target does not match arguments.child_id"
                )
        elif binding.target != arguments.task_id:
            raise _PrivateAdapterRejected(
                "binding.target does not match arguments.task_id"
            )

        return operation

    def _dispatch_native(
        self,
        operation: str,
        binding: CapabilityBinding,
        arguments: Any,
        *,
        allow_nested: bool = False,
    ) -> Any:
        if operation == "kanban_create":
            with _kb._scoped_mutation_authority(
                _kb._MUTATION_AUTHORITY_DISPATCHER_ORCHESTRATOR
            ):
                return _kb.create_task(
                    self._conn,
                    title=arguments.title,
                    body=arguments.body,
                    assignee=arguments.assignee,
                    parents=arguments.parents,
                    tenant=arguments.tenant,
                    priority=arguments.priority,
                    workspace_kind=arguments.workspace_kind,
                    workspace_path=arguments.workspace_path,
                    project_id=arguments.project,
                    model_override=arguments.model,
                    provider_override=arguments.provider,
                    goal_mode=arguments.goal_mode,
                    goal_max_turns=arguments.goal_max_turns,
                    session_id=binding.session_id,
                    board=arguments.board,
                    created_by="adrian-kanban",
                    _task_id=arguments.task_id,
                )
        elif operation == "kanban_complete":
            completed = _kb.complete_task(
                self._conn,
                arguments.task_id,
                result=arguments.result,
                summary=arguments.summary,
                metadata=arguments.metadata,
                created_cards=arguments.created_cards,
                expected_run_id=arguments.expected_run_id,
                fire_lifecycle_hook=False,
                _allow_nested=allow_nested,
                _defer_cleanup=allow_nested,
            )
            if allow_nested and completed is True:
                self._deferred_cleanup_task_ids.append(arguments.task_id)
            return completed
        elif operation == "kanban_block":
            return _kb.block_task(
                self._conn,
                arguments.task_id,
                reason=arguments.reason,
                kind=arguments.kind,
                expected_run_id=arguments.expected_run_id,
                _allow_nested=allow_nested,
            )
        elif operation == "kanban_unblock":
            return _kb.unblock_task(
                self._conn,
                arguments.task_id,
                _allow_nested=allow_nested,
            )
        elif operation == "kanban_comment":
            return _kb.add_comment(
                self._conn,
                arguments.task_id,
                arguments.author,
                arguments.body,
            )
        elif operation == "kanban_heartbeat":
            if not _kb.heartbeat_claim(
                self._conn,
                arguments.task_id,
                claimer=arguments.claim_lock,
                _allow_nested=allow_nested,
            ):
                return False
            return _kb.heartbeat_worker(
                self._conn,
                arguments.task_id,
                note=arguments.note,
                expected_run_id=arguments.expected_run_id,
                _allow_nested=allow_nested,
            )
        elif operation == "kanban_request_changes":
            return _kb.request_changes(
                self._conn,
                arguments.task_id,
                reason=arguments.reason,
                expected_run_id=arguments.expected_run_id,
                _allow_nested=allow_nested,
            )
        elif operation == "kanban_request_review":
            return _kb.request_review(
                self._conn,
                arguments.task_id,
                summary=arguments.summary,
                metadata=arguments.metadata,
                reviewer=arguments.reviewer,
                expected_run_id=arguments.expected_run_id,
                force=arguments.force,
                with_reason=arguments.with_reason,
                _allow_nested=allow_nested,
            )
        elif operation == "kanban_link":
            return _kb.link_tasks(
                self._conn,
                arguments.parent_id,
                arguments.child_id,
                _allow_nested=allow_nested,
            )
        elif operation in ("kanban_attach", "kanban_attach_url"):
            attachment_id = _kb.store_attachment_bytes(
                self._conn,
                arguments.task_id,
                arguments.filename,
                arguments.data,
                content_type=arguments.content_type,
                uploaded_by=arguments.uploaded_by,
                board=arguments.board,
                _allow_nested=allow_nested,
            )
            row = _kb.get_attachment(self._conn, attachment_id)
            if (
                row is None
                or row.task_id != arguments.task_id
                or not row.stored_path
                or not row.stored_path.strip()
            ):
                raise _PrivateAdapterRejected("attachment validation failed")
            self._rollback_attachment_paths.append(row.stored_path)
            return attachment_id
        raise _PrivateAdapterRejected("unreachable")

    def execute(
        self, capability: Any, binding: CapabilityBinding, arguments: Any
    ) -> Any:
        operation = self._validate_binding_and_arguments(capability, binding, arguments)

        if operation == "kanban_launch":
            with _capability_scope(
                self._provider,
                self._conn,
                capability,
                binding,
                allow_multiple_writes=True,
            ):
                return _kb._launch_admitted_task(
                    self._conn,
                    arguments.task_id,
                    expected_assignee=arguments.expected_assignee,
                    spawn_fn=self._spawn_fn,
                    ttl_seconds=arguments.ttl_seconds,
                    failure_limit=arguments.failure_limit,
                    board=arguments.board,
                )

        with _capability_scope(self._provider, self._conn, capability, binding):
            return self._dispatch_native(operation, binding, arguments)

    def _execute_in_active_transaction(
        self, capability: Any, binding: CapabilityBinding, arguments: Any
    ) -> Any:
        if (
            self._active_capability is None
            or self._active_binding is None
            or not self._conn.in_transaction
        ):
            raise _PrivateAdapterRejected("active boundary transaction required")
        if capability is not self._active_capability:
            raise _PrivateAdapterRejected("active boundary transaction required")
        if binding is not self._active_binding:
            raise _PrivateAdapterRejected("active boundary transaction required")

        operation = self._validate_binding_and_arguments(capability, binding, arguments)
        if operation not in _ACTIVE_TRANSACTION_OPERATIONS:
            raise _PrivateAdapterRejected(
                "operation is not admitted inside the active boundary transaction"
            )

        return self._dispatch_native(operation, binding, arguments, allow_nested=True)

    def _complete_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        result: Optional[str] = None,
        summary: Optional[str] = None,
        metadata: Optional[dict] = None,
        created_cards: Optional[tuple[str, ...]] = None,
        expected_run_id: Optional[int] = None,
    ) -> Any:
        args = _CompleteTaskArgs(
            task_id=task_id,
            result=result,
            summary=summary,
            metadata=metadata,
            created_cards=created_cards,
            expected_run_id=expected_run_id,
        )
        return self._execute_in_active_transaction(capability, binding, args)

    def _block_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        reason: Optional[str] = None,
        kind: Optional[str] = None,
        expected_run_id: Optional[int] = None,
    ) -> Any:
        args = _BlockTaskArgs(
            task_id=task_id,
            reason=reason,
            kind=kind,
            expected_run_id=expected_run_id,
        )
        return self._execute_in_active_transaction(capability, binding, args)

    def _unblock_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
    ) -> Any:
        args = _UnblockTaskArgs(task_id=task_id)
        return self._execute_in_active_transaction(capability, binding, args)

    def _comment_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        author: str,
        body: str,
    ) -> Any:
        args = _CommentArgs(task_id=task_id, author=author, body=body)
        return self._execute_in_active_transaction(capability, binding, args)

    def _heartbeat_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        claim_lock: str,
        note: Optional[str] = None,
        expected_run_id: Optional[int] = None,
    ) -> Any:
        args = _HeartbeatArgs(
            task_id=task_id,
            claim_lock=claim_lock,
            note=note,
            expected_run_id=expected_run_id,
        )
        return self._execute_in_active_transaction(capability, binding, args)

    def _request_changes_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        reason: str,
        expected_run_id: Optional[int] = None,
    ) -> Any:
        args = _RequestChangesArgs(
            task_id=task_id,
            reason=reason,
            expected_run_id=expected_run_id,
        )
        return self._execute_in_active_transaction(capability, binding, args)

    def _request_review_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        summary: Optional[str] = None,
        metadata: Optional[dict] = None,
        reviewer: Optional[str] = None,
        expected_run_id: Optional[int] = None,
        force: bool = False,
        with_reason: bool = False,
    ) -> Any:
        args = _RequestReviewArgs(
            task_id=task_id,
            summary=summary,
            metadata=metadata,
            reviewer=reviewer,
            expected_run_id=expected_run_id,
            force=force,
            with_reason=with_reason,
        )
        return self._execute_in_active_transaction(capability, binding, args)

    def _create_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        title: str,
        assignee: str,
        body: Optional[str] = None,
        parents: tuple[str, ...] = (),
        tenant: Optional[str] = None,
        priority: int = 0,
        workspace_kind: str = "scratch",
        workspace_path: Optional[str] = None,
        project: Optional[str] = None,
        goal_mode: bool = False,
        goal_max_turns: Optional[int] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        board: Optional[str] = None,
    ) -> Any:
        args = _CreateTaskArgs(
            task_id=task_id,
            title=title,
            assignee=assignee,
            body=body,
            parents=parents,
            tenant=tenant,
            priority=priority,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            project=project,
            goal_mode=goal_mode,
            goal_max_turns=goal_max_turns,
            model=model,
            provider=provider,
            board=board,
        )
        return self._execute_in_active_transaction(capability, binding, args)

    def _create_for_purge_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        title: str,
        assignee: str,
        body: Optional[str] = None,
        parents: tuple[str, ...] = (),
        tenant: Optional[str] = None,
        priority: int = 0,
        workspace_kind: str = "scratch",
        workspace_path: Optional[str] = None,
        project: Optional[str] = None,
        goal_mode: bool = False,
        goal_max_turns: Optional[int] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        board: Optional[str] = None,
        initiative_id: str,
    ) -> Any:
        if (
            self._active_capability is None
            or self._active_binding is None
            or not self._conn.in_transaction
        ):
            raise _PrivateAdapterRejected("active boundary transaction required")
        if capability is not self._active_capability:
            raise _PrivateAdapterRejected("active boundary transaction required")
        if binding is not self._active_binding:
            raise _PrivateAdapterRejected("active boundary transaction required")
        if binding.operation != "kanban_update_initiative":
            raise _PrivateAdapterRejected(
                "binding.operation must be kanban_update_initiative"
            )
        if binding.actor_profile != "default":
            raise _PrivateAdapterRejected("actor_profile must be default")
        if binding.target != initiative_id:
            raise _PrivateAdapterRejected(
                "binding.target does not match initiative_id"
            )
        if not _is_nonblank_str(board):
            raise _PrivateAdapterRejected("board must be a nonblank string")
        row = self._conn.execute(
            "SELECT id FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND board_slug = ? "
            "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
            (initiative_id, board),
        ).fetchone()
        if row is None:
            raise _PrivateAdapterRejected("open initiative card not found")
        args = _CreateTaskArgs(
            task_id=task_id,
            title=title,
            assignee=assignee,
            body=body,
            parents=parents,
            tenant=tenant,
            priority=priority,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            project=project,
            goal_mode=goal_mode,
            goal_max_turns=goal_max_turns,
            model=model,
            provider=provider,
            board=board,
        )
        return self._dispatch_native("kanban_create", binding, args, allow_nested=True)

    def _attach_for_purge_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        filename: str,
        data: bytes,
        content_type: Optional[str] = None,
        board: Optional[str] = None,
        initiative_id: str,
        expected_successor_task_id: str,
    ) -> int:
        if (
            self._active_capability is None
            or self._active_binding is None
            or not self._conn.in_transaction
        ):
            raise _PrivateAdapterRejected("active boundary transaction required")
        if capability is not self._active_capability:
            raise _PrivateAdapterRejected("active boundary transaction required")
        if binding is not self._active_binding:
            raise _PrivateAdapterRejected("active boundary transaction required")
        if binding.operation != "kanban_update_initiative":
            raise _PrivateAdapterRejected(
                "binding.operation must be kanban_update_initiative"
            )
        if binding.actor_profile != "default":
            raise _PrivateAdapterRejected("actor_profile must be default")
        if binding.target != initiative_id:
            raise _PrivateAdapterRejected(
                "binding.target does not match initiative_id"
            )
        if not _is_nonblank_str(board):
            raise _PrivateAdapterRejected("board must be a nonblank string")
        card = self._conn.execute(
            "SELECT id, initiative_id FROM adrian_kanban_cards "
            "WHERE task_id = ? AND board_slug = ? AND card_type = 'task'",
            (task_id, board),
        ).fetchone()
        if card is None:
            raise _PrivateAdapterRejected("successor task card not found")
        if card["initiative_id"] != initiative_id:
            raise _PrivateAdapterRejected(
                "successor task does not belong to the bound initiative"
            )
        if task_id != expected_successor_task_id:
            raise _PrivateAdapterRejected(
                "task_id does not match expected successor task"
            )
        row = self._conn.execute(
            "SELECT id FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND board_slug = ? "
            "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
            (initiative_id, board),
        ).fetchone()
        if row is None:
            raise _PrivateAdapterRejected("open initiative card not found")
        args = _AttachArgs(
            task_id=task_id,
            filename=filename,
            data=data,
            content_type=content_type,
            board=board,
        )
        return self._dispatch_native("kanban_attach", binding, args, allow_nested=True)

    def _link_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        parent_id: str,
        child_id: str,
    ) -> Any:
        args = _LinkArgs(parent_id=parent_id, child_id=child_id)
        return self._execute_in_active_transaction(capability, binding, args)

    def _attach_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        filename: str,
        data: bytes,
        content_type: Optional[str] = None,
        board: Optional[str] = None,
    ) -> Any:
        args = _AttachArgs(
            task_id=task_id,
            filename=filename,
            data=data,
            content_type=content_type,
            board=board,
        )
        return self._execute_in_active_transaction(capability, binding, args)

    def _attach_during_create_in_active_transaction(
        self,
        capability: Any,
        binding: CapabilityBinding,
        *,
        task_id: str,
        filename: str,
        data: bytes,
        content_type: Optional[str] = None,
        board: Optional[str] = None,
    ) -> int:
        if (
            self._active_capability is None
            or self._active_binding is None
            or not self._conn.in_transaction
        ):
            raise _PrivateAdapterRejected("active boundary transaction required")
        if capability is not self._active_capability:
            raise _PrivateAdapterRejected("active boundary transaction required")
        if binding is not self._active_binding:
            raise _PrivateAdapterRejected("active boundary transaction required")
        if binding.operation != "kanban_create" or binding.target != task_id:
            raise _PrivateAdapterRejected("binding must target the created task")
        if _kb.get_task(self._conn, task_id) is None:
            raise _PrivateAdapterRejected("task does not exist")
        args = _AttachArgs(
            task_id=task_id,
            filename=filename,
            data=data,
            content_type=content_type,
            board=board,
        )
        return self._dispatch_native("kanban_attach", binding, args, allow_nested=True)

    @contextlib.contextmanager
    def mutation_transaction(self, capability: Any, binding: CapabilityBinding):
        if type(binding) is not CapabilityBinding:
            raise _PrivateAdapterRejected("binding must be a CapabilityBinding")
        if self._active_capability is not None or self._active_binding is not None:
            raise _PrivateAdapterRejected("active boundary transaction required")
        self._deferred_cleanup_task_ids = []
        self._rollback_attachment_paths = []
        self._active_capability = capability
        self._active_binding = binding
        try:
            with _capability_scope(self._provider, self._conn, capability, binding):
                try:
                    with _kb.write_txn(self._conn):
                        yield self._conn
                except BaseException:
                    rollback_paths = list(self._rollback_attachment_paths)
                    for path in rollback_paths:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                    raise
                self._rollback_attachment_paths = []
                for task_id in self._deferred_cleanup_task_ids:
                    try:
                        _kb._cleanup_workspace(self._conn, task_id)
                    except Exception:
                        pass
        finally:
            self._active_capability = None
            self._active_binding = None
            self._deferred_cleanup_task_ids = []
            self._rollback_attachment_paths = []

import contextlib
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


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool) and value > 0


def _is_optional_positive_int(value: Any) -> bool:
    return value is None or _is_positive_int(value)


def _is_exact_dict(value: Any) -> bool:
    return type(value) is dict


def _is_optional_dict(value: Any) -> bool:
    return value is None or _is_exact_dict(value)


def _is_exact_bool(value: Any) -> bool:
    return type(value) is bool


def _is_optional_tuple_of_unique_nonblank_strs(value: Any) -> bool:
    if value is None:
        return True
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
    note: Optional[str] = None
    expected_run_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _PrivateAdapterRejected("task_id must be a nonblank string")
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
            raise _PrivateAdapterRejected(
                "expected_assignee must be a nonblank string"
            )
        if self.board is not None and not (
            _is_exact_str(self.board) and self.board.strip() != ""
        ):
            raise _PrivateAdapterRejected(
                "board must be None or a nonblank string"
            )
        if not _is_optional_positive_int(self.ttl_seconds):
            raise _PrivateAdapterRejected(
                "ttl_seconds must be None or a positive integer"
            )
        if not _is_positive_int(self.failure_limit):
            raise _PrivateAdapterRejected(
                "failure_limit must be a positive integer"
            )


_OPERATION_ARGUMENT_TYPES = MappingProxyType(
    {
        "kanban_complete": _CompleteTaskArgs,
        "kanban_block": _BlockTaskArgs,
        "kanban_unblock": _UnblockTaskArgs,
        "kanban_comment": _CommentArgs,
        "kanban_heartbeat": _HeartbeatArgs,
        "kanban_request_changes": _RequestChangesArgs,
        "kanban_request_review": _RequestReviewArgs,
        "kanban_launch": _LaunchTaskArgs,
    }
)


# Only operations whose native mutators already use nested savepoint
# semantics may be admitted inside the boundary's outer transaction. Any
# other operation is rejected explicitly rather than falsely claimed safe.
_ACTIVE_TRANSACTION_OPERATIONS = frozenset(
    {
        "kanban_complete",
        "kanban_block",
        "kanban_unblock",
        "kanban_comment",
        "kanban_heartbeat",
        "kanban_request_changes",
        "kanban_request_review",
    }
)


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

        if binding.target != arguments.task_id:
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
        if operation == "kanban_complete":
            return _kb.complete_task(
                self._conn,
                arguments.task_id,
                result=arguments.result,
                summary=arguments.summary,
                metadata=arguments.metadata,
                created_cards=arguments.created_cards,
                expected_run_id=arguments.expected_run_id,
                fire_lifecycle_hook=False,
                _allow_nested=allow_nested,
            )
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
        raise _PrivateAdapterRejected("unreachable")

    def execute(
        self, capability: Any, binding: CapabilityBinding, arguments: Any
    ) -> Any:
        operation = self._validate_binding_and_arguments(
            capability, binding, arguments
        )

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

        operation = self._validate_binding_and_arguments(
            capability, binding, arguments
        )
        if operation not in _ACTIVE_TRANSACTION_OPERATIONS:
            raise _PrivateAdapterRejected(
                "operation is not admitted inside the active boundary transaction"
            )

        return self._dispatch_native(
            operation, binding, arguments, allow_nested=True
        )

    @contextlib.contextmanager
    def mutation_transaction(
        self, capability: Any, binding: CapabilityBinding
    ):
        if type(binding) is not CapabilityBinding:
            raise _PrivateAdapterRejected("binding must be a CapabilityBinding")
        if self._active_capability is not None or self._active_binding is not None:
            raise _PrivateAdapterRejected("active boundary transaction required")
        self._active_capability = capability
        self._active_binding = binding
        try:
            with _capability_scope(
                self._provider, self._conn, capability, binding
            ):
                with _kb.write_txn(self._conn):
                    yield self._conn
        finally:
            self._active_capability = None
            self._active_binding = None

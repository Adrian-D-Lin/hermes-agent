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


_OPERATION_ARGUMENT_TYPES = MappingProxyType(
    {
        "kanban_complete": _CompleteTaskArgs,
        "kanban_block": _BlockTaskArgs,
        "kanban_unblock": _UnblockTaskArgs,
        "kanban_comment": _CommentArgs,
        "kanban_heartbeat": _HeartbeatArgs,
        "kanban_request_changes": _RequestChangesArgs,
        "kanban_request_review": _RequestReviewArgs,
    }
)


class _PrivateNativeAdapter:
    def __init__(
        self, provider: AdrianKanbanAuthorityProvider, conn: sqlite3.Connection
    ) -> None:
        if type(provider) is not AdrianKanbanAuthorityProvider:
            raise _PrivateAdapterRejected(
                "provider must be an AdrianKanbanAuthorityProvider"
            )
        if type(conn) is not sqlite3.Connection:
            raise _PrivateAdapterRejected("conn must be a sqlite3.Connection")
        self._provider = provider
        self._conn = conn

    def execute(
        self, capability: Any, binding: CapabilityBinding, arguments: Any
    ) -> Any:
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

        with _capability_scope(
            self._provider, self._conn, capability, binding
        ):
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
                )
            elif operation == "kanban_block":
                return _kb.block_task(
                    self._conn,
                    arguments.task_id,
                    reason=arguments.reason,
                    kind=arguments.kind,
                    expected_run_id=arguments.expected_run_id,
                )
            elif operation == "kanban_unblock":
                return _kb.unblock_task(self._conn, arguments.task_id)
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
                )
            elif operation == "kanban_request_changes":
                return _kb.request_changes(
                    self._conn,
                    arguments.task_id,
                    reason=arguments.reason,
                    expected_run_id=arguments.expected_run_id,
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
                )

        raise _PrivateAdapterRejected("unreachable")

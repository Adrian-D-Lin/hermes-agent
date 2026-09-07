from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from types import MappingProxyType
from typing import Any

from .capability import CapabilityBinding
from .diagnostics import (
    Boundary,
    DiagnosticCollector,
    FailedCheck,
)
from .provider import (
    AdrianKanbanAuthorityProvider,
    PLUGIN_VERSION,
    PROTOCOL_VERSION,
)

# Public operation taxonomy. These sets are frozen by the ratified Canon
# operation map and are consumed verbatim by every later S3 slice.
READ_ONLY_OPERATIONS = frozenset(
    {
        "kanban_show",
        "kanban_list",
        "kanban_attachments",
    }
)

ORDINARY_TASK_OPERATIONS = frozenset(
    {
        "kanban_create",
        "kanban_complete",
        "kanban_block",
        "kanban_unblock",
        "kanban_comment",
        "kanban_link",
        "kanban_heartbeat",
        "kanban_attach",
        "kanban_attach_url",
        "kanban_request_changes",
        "kanban_request_review",
    }
)

INITIATIVE_OPERATIONS = frozenset(
    {
        "kanban_create_initiative",
        "kanban_update_initiative",
        "kanban_transition_initiative",
        "kanban_close_initiative",
    }
)

RECOGNIZED_OPERATIONS = (
    READ_ONLY_OPERATIONS | ORDINARY_TASK_OPERATIONS | INITIATIVE_OPERATIONS
)

_UNRECOGNIZED_OPERATION = "UNRECOGNIZED_OPERATION"
_COMMAND_BOUNDARY_UNAVAILABLE = "COMMAND_BOUNDARY_UNAVAILABLE"
_OPERATION_NOT_IMPLEMENTED = "OPERATION_NOT_IMPLEMENTED"
_COMMAND_EXECUTION_FAILED = "COMMAND_EXECUTION_FAILED"

_BOUNDARY_SOURCE = "adrian-kanban"
_BOUNDARY_DESTINATION = "adrian-kanban"


def _resolve_attempt_id(fields: dict[str, Any]) -> str:
    """Return the caller's attempt_id only when it is a nonblank string.

    Any missing, blank, or non-string value yields an opaque nonblank
    identifier. Authority is never derived from other caller fields.
    """
    attempt_id = fields.get("attempt_id")
    if isinstance(attempt_id, str):
        trimmed = attempt_id.strip()
        if trimmed:
            return trimmed
    return "attempt-" + uuid.uuid4().hex


def _rejection(
    attempt_id: str,
    operation: str,
    code: str,
) -> dict[str, Any]:
    """Build a canonical fail-closed rejection envelope.

    This slice is deliberately dark: it binds no runtime, so every call
    fails closed through the shared diagnostic envelope rather than a
    competing hand-maintained format.
    """
    collector = DiagnosticCollector(
        attempt_id=attempt_id,
        operation=operation,
        boundary=Boundary(
            source=_BOUNDARY_SOURCE,
            destination=_BOUNDARY_DESTINATION,
        ),
    )
    collector.failure(
        FailedCheck(
            code=code,
            target=operation,
            expected=(
                "a recognized operation admitted by the shared command boundary"
            ),
            observed=operation,
            accepted_format=(
                "one of READ_ONLY_OPERATIONS, ORDINARY_TASK_OPERATIONS, or "
                "INITIATIVE_OPERATIONS"
            ),
            remediation=(
                "submit a recognized operation through the plugin command "
                "boundary; no native mutation, dispatch, or notification route "
                "is available while this boundary is selected"
            ),
            responsible_actor="session_agent",
            retry="same_operation",
        )
    )
    return collector.rejection().as_dict()


def command_boundary(action: str, **fields: Any) -> dict[str, Any]:
    """Shared Kanban command boundary.

    This foundation fixes the public operation taxonomy and the boundary's
    fail-closed, unbound behavior. It binds no runtime: an unrecognized
    operation returns ``UNRECOGNIZED_OPERATION`` and a recognized operation
    returns ``COMMAND_BOUNDARY_UNAVAILABLE``. Both are rendered through the
    canonical ``RejectionEnvelope`` so every surface reports identical
    diagnostics.
    """
    attempt_id = _resolve_attempt_id(fields)
    if action not in RECOGNIZED_OPERATIONS:
        return _rejection(attempt_id, action, _UNRECOGNIZED_OPERATION)
    return _rejection(attempt_id, action, _COMMAND_BOUNDARY_UNAVAILABLE)


class _CommandContext:
    def __init__(
        self,
        operation: str,
        payload: dict[str, Any],
        connection: sqlite3.Connection,
        attempt_id: str,
        capability: Any = None,
        binding: Any = None,
        mutation_executor: Any = None,
    ) -> None:
        self.operation = operation
        self.payload = payload
        self.connection = connection
        self.attempt_id = attempt_id
        self.capability = capability
        self.binding = binding
        self.mutation_executor = mutation_executor


class _CommandBoundary:
    def __init__(
        self,
        *,
        database_path: str,
        provider: AdrianKanbanAuthorityProvider,
        handlers: dict[str, Any],
    ) -> None:
        if type(provider) is not AdrianKanbanAuthorityProvider:
            raise TypeError("provider must be an AdrianKanbanAuthorityProvider")
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("database_path must be a nonblank string")
        self._database_path = database_path
        self._provider = provider
        self._handlers = MappingProxyType(dict(handlers))

    def submit(self, action: str, **fields: Any) -> dict[str, Any]:
        attempt_id = _resolve_attempt_id(fields)
        if action not in RECOGNIZED_OPERATIONS:
            return _rejection(attempt_id, action, _UNRECOGNIZED_OPERATION)

        handler = self._handlers.get(action)
        if handler is None:
            return self._rejection_internal(
                attempt_id, action, _OPERATION_NOT_IMPLEMENTED
            )

        if action in READ_ONLY_OPERATIONS:
            return self._execute_read(action, handler, attempt_id, fields)
        else:
            return self._execute_mutation(action, handler, attempt_id, fields)

    def _execute_read(
        self,
        action: str,
        handler: Any,
        attempt_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        payload = fields.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        try:
            conn = sqlite3.connect(self._database_path)
            conn.row_factory = sqlite3.Row
        except Exception:
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        try:
            context = _CommandContext(
                operation=action,
                payload=payload,
                connection=conn,
                attempt_id=attempt_id,
            )
            result = handler(context)
            if not isinstance(result, dict):
                raise TypeError("handler must return a dict")
            return {
                "result": "ACCEPTED",
                "state_changed": False,
                "attempt_id": attempt_id,
                "operation": action,
                "value": result,
            }
        except Exception:
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        finally:
            conn.close()

    def _execute_mutation(
        self,
        action: str,
        handler: Any,
        attempt_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        target = fields.get("target")
        session_id = fields.get("session_id")
        execution_context = fields.get("execution_context")
        expected_version = fields.get("expected_version")
        workspace_id = fields.get("workspace_id")
        payload = fields.get("payload")

        if not isinstance(target, str) or not target.strip():
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        if not isinstance(session_id, str) or not session_id.strip():
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        if not isinstance(execution_context, str) or not execution_context.strip():
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        if workspace_id is not None and (
            not isinstance(workspace_id, str) or not workspace_id.strip()
        ):
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        if not isinstance(payload, dict):
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )

        canonical_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

        binding = CapabilityBinding(
            operation=action,
            target=target,
            expected_version=expected_version,
            canonical_digest=canonical_digest,
            session_id=session_id,
            workspace_id=workspace_id,
            plugin_version=PLUGIN_VERSION,
            protocol_version=PROTOCOL_VERSION,
            execution_context=execution_context,
        )

        try:
            capability = self._provider._mint_after_admission(binding)
        except Exception:
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )

        try:
            conn = sqlite3.connect(self._database_path)
            conn.row_factory = sqlite3.Row
        except Exception:
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )

        adapter = self._provider._create_mutation_executor(conn)
        try:
            with adapter.mutation_transaction(capability, binding):
                context = _CommandContext(
                    operation=action,
                    payload=payload,
                    connection=conn,
                    attempt_id=attempt_id,
                    capability=capability,
                    binding=binding,
                    mutation_executor=adapter,
                )
                result = handler(context)
                if not isinstance(result, dict):
                    raise TypeError("handler must return a dict")
                return {
                    "result": "ACCEPTED",
                    "state_changed": True,
                    "attempt_id": attempt_id,
                    "operation": action,
                    "value": result,
                }
        except Exception:
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        finally:
            conn.close()

    def _rejection_internal(
        self, attempt_id: str, operation: str, code: str
    ) -> dict[str, Any]:
        collector = DiagnosticCollector(
            attempt_id=attempt_id,
            operation=operation,
            boundary=Boundary(
                source=_BOUNDARY_SOURCE,
                destination=_BOUNDARY_DESTINATION,
            ),
        )
        collector.failure(
            FailedCheck(
                code=code,
                target=operation,
                expected="a valid command execution",
                observed=operation,
                accepted_format="recognized operation with valid fields",
                remediation="correct the command fields and retry",
                responsible_actor="session_agent",
                retry="same_operation",
            )
        )
        return collector.rejection().as_dict()


__all__ = [
    "READ_ONLY_OPERATIONS",
    "ORDINARY_TASK_OPERATIONS",
    "INITIATIVE_OPERATIONS",
    "RECOGNIZED_OPERATIONS",
    "command_boundary",
    "_CommandBoundary",
]

from __future__ import annotations

import uuid
from typing import Any

from .diagnostics import (
    Boundary,
    DiagnosticCollector,
    FailedCheck,
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


__all__ = [
    "READ_ONLY_OPERATIONS",
    "ORDINARY_TASK_OPERATIONS",
    "INITIATIVE_OPERATIONS",
    "RECOGNIZED_OPERATIONS",
    "command_boundary",
]

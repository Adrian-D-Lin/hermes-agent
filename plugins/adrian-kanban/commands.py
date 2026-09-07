from __future__ import annotations

import hashlib
import json
import sqlite3
import time
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

TOOL_SCHEMAS: dict[str, Any] = {
    "kanban_show": {
        "name": "kanban_show",
        "description": (
            "Read a single kanban task by its immutable identifier."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to show.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the read.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    "kanban_list": {
        "name": "kanban_list",
        "description": (
            "List kanban tasks within a scope without mutating state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": (
                        "Optional initiative scope to list tasks within."
                    ),
                },
                "assignee": {
                    "type": "string",
                    "description": "Optional assignee filter.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter.",
                },
                "tenant": {
                    "type": "string",
                    "description": "Optional tenant scope.",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": (
                        "Optional flag to include archived tasks."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional maximum number of tasks to return.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the read.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "kanban_attachments": {
        "name": "kanban_attachments",
        "description": (
            "Read the attachments associated with a kanban task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to inspect.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the read.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    "kanban_create": {
        "name": "kanban_create",
        "description": (
            "Create a new ordinary kanban task at both identity levels."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the new task.",
                },
                "initiative_id": {
                    "type": "string",
                    "description": "Initiative the new task belongs to.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable task title.",
                },
                "assignee": {
                    "type": "string",
                    "description": "Assignee responsible for the task.",
                },
                "body": {
                    "type": "string",
                    "description": "Free-form task body or rationale.",
                },
                "parents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional parent task identifiers.",
                },
                "tenant": {
                    "type": "string",
                    "description": "Optional tenant scope.",
                },
                "priority": {
                    "type": "integer",
                    "description": "Optional task priority.",
                },
                "workspace_kind": {
                    "type": "string",
                    "enum": ["scratch", "dir", "worktree"],
                    "description": "Optional workspace kind.",
                },
                "workspace_path": {
                    "type": "string",
                    "description": "Optional workspace path.",
                },
                "project": {
                    "type": "string",
                    "description": "Optional project reference.",
                },
                "goal_mode": {
                    "type": "boolean",
                    "description": "Optional goal mode.",
                },
                "goal_max_turns": {
                    "type": "integer",
                    "description": "Optional maximum goal turns.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model reference.",
                },
                "provider": {
                    "type": "string",
                    "description": "Optional provider reference.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": [
                "task_id",
                "initiative_id",
                "title",
                "assignee",
                "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
    "kanban_complete": {
        "name": "kanban_complete",
        "description": (
            "Mark an ordinary kanban task as complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to complete.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional completion summary.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional completion metadata.",
                },
                "result": {
                    "type": "string",
                    "description": "Optional completion result.",
                },
                "created_cards": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional created card references.",
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional completion artifacts.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_block": {
        "name": "kanban_block",
        "description": (
            "Block an ordinary kanban task, halting its forward progress."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to block.",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for blocking the task.",
                },
                "kind": {
                    "type": "string",
                    "enum": [
                        "dependency",
                        "needs_input",
                        "capability",
                        "transient",
                    ],
                    "description": "Kind of block to apply.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "reason", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_unblock": {
        "name": "kanban_unblock",
        "description": (
            "Unblock an ordinary kanban task, resuming its forward progress."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to unblock.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_comment": {
        "name": "kanban_comment",
        "description": (
            "Attach a comment to an ordinary kanban task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to comment on.",
                },
                "body": {
                    "type": "string",
                    "description": "Comment body text.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "body", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_link": {
        "name": "kanban_link",
        "description": (
            "Link an ordinary kanban task to external references."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "parent_id": {
                    "type": "string",
                    "description": "Immutable identifier of the parent task.",
                },
                "child_id": {
                    "type": "string",
                    "description": "Immutable identifier of the child task.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["parent_id", "child_id", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_heartbeat": {
        "name": "kanban_heartbeat",
        "description": (
            "Send a heartbeat for an ordinary kanban task to signal liveness."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to heartbeat.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional heartbeat note.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_attach": {
        "name": "kanban_attach",
        "description": (
            "Attach a local artifact to an ordinary kanban task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to attach to.",
                },
                "filename": {
                    "type": "string",
                    "description": "Filename of the local artifact.",
                },
                "content_base64": {
                    "type": "string",
                    "description": "Base64-encoded artifact content.",
                },
                "content_type": {
                    "type": "string",
                    "description": "MIME content type of the artifact.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "filename", "content_base64", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_attach_url": {
        "name": "kanban_attach_url",
        "description": (
            "Attach a remote URL to an ordinary kanban task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to attach to.",
                },
                "url": {
                    "type": "string",
                    "description": "Remote URL to attach.",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional filename for the remote artifact.",
                },
                "content_type": {
                    "type": "string",
                    "description": "Optional MIME content type of the artifact.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "url", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_request_changes": {
        "name": "kanban_request_changes",
        "description": (
            "Request changes to an ordinary kanban task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to revise.",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason requesting the changes.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "reason", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_request_review": {
        "name": "kanban_request_review",
        "description": (
            "Request a review of an ordinary kanban task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to review.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional review summary.",
                },
                "reviewer": {
                    "type": "string",
                    "description": "Optional reviewer reference.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional review metadata.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": ["task_id", "summary", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    "kanban_create_initiative": {
        "name": "kanban_create_initiative",
        "description": (
            "Create a new kanban initiative."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "Immutable identifier of the new initiative.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable initiative title.",
                },
                "body": {
                    "type": "string",
                    "description": "Free-form initiative body or rationale.",
                },
                "approval_id": {
                    "type": "string",
                    "description": "Approval reference authorizing creation.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": [
                "initiative_id",
                "title",
                "body",
                "approval_id",
                "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
    "kanban_update_initiative": {
        "name": "kanban_update_initiative",
        "description": (
            "Update an existing kanban initiative."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "Immutable identifier of the initiative to update.",
                },
                "update_kind": {
                    "type": "string",
                    "description": "Kind of update to apply to the initiative.",
                },
                "update": {
                    "type": "object",
                    "description": "Update payload describing the change.",
                },
                "approval_id": {
                    "type": "string",
                    "description": "Approval reference authorizing the update.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": [
                "initiative_id",
                "update_kind",
                "update",
                "approval_id",
                "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
    "kanban_transition_initiative": {
        "name": "kanban_transition_initiative",
        "description": (
            "Transition an existing kanban initiative to a new phase."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": (
                        "Immutable identifier of the initiative to transition."
                    ),
                },
                "to_phase": {
                    "type": "string",
                    "description": "Target phase for the initiative.",
                },
                "reconciliation_ref": {
                    "type": "string",
                    "description": (
                        "Reconciliation reference backing the transition."
                    ),
                },
                "approval_id": {
                    "type": "string",
                    "description": "Approval reference authorizing the transition.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": [
                "initiative_id",
                "to_phase",
                "reconciliation_ref",
                "approval_id",
                "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
    "kanban_close_initiative": {
        "name": "kanban_close_initiative",
        "description": (
            "Close an existing kanban initiative."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "Immutable identifier of the initiative to close.",
                },
                "closure_result_ref": {
                    "type": "string",
                    "description": (
                        "Reference describing the closure outcome."
                    ),
                },
                "approval_id": {
                    "type": "string",
                    "description": "Approval reference authorizing closure.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Caller-supplied key guaranteeing safe replay."
                    ),
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Optional caller attempt identifier.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the write.",
                },
            },
            "required": [
                "initiative_id",
                "closure_result_ref",
                "approval_id",
                "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
}

TOOL_SCHEMAS = MappingProxyType(dict(TOOL_SCHEMAS))


def _handle_link(context: Any) -> dict[str, Any]:
    parent_id = context.payload.get("parent_id")
    child_id = context.payload.get("child_id")
    if not (type(parent_id) is str and parent_id.strip()):
        raise ValueError("parent_id must be a nonblank string")
    if not (type(child_id) is str and child_id.strip()):
        raise ValueError("child_id must be a nonblank string")
    if parent_id == child_id:
        raise ValueError("a task cannot link to itself")

    def _validate_endpoint(task_id: str) -> None:
        row = context.connection.execute(
            "SELECT card_type, task_id, initiative_id FROM adrian_kanban_cards "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["card_type"] != "task" or row["task_id"] != task_id:
            raise ValueError(f"{task_id} is not a valid task endpoint")
        init_row = context.connection.execute(
            "SELECT card_type, task_id FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND task_id IS NULL",
            (row["initiative_id"],),
        ).fetchone()
        if init_row is None or init_row["card_type"] != "initiative":
            raise ValueError(
                f"initiative for {task_id} has no canonical initiative card"
            )

    _validate_endpoint(parent_id)
    _validate_endpoint(child_id)

    context.mutation_executor._link_in_active_transaction(
        context.capability,
        context.binding,
        parent_id=parent_id,
        child_id=child_id,
    )
    return {"parent_id": parent_id, "child_id": child_id}


def register_public_tools(ctx: Any, boundary: Any) -> None:
    """Register the 18 ratified public model-tools on the given toolset.

    Each tool is bound to a distinct handler closure that delegates to the
    injected boundary's ``submit`` for its fixed operation. The boundary is
    captured only by construction-time closures; no module-global state is
    stored.
    """

    def _make_handler(operation: str):
        def _handler(args: dict[str, Any], **_runtime_fields: Any) -> str:
            copied = dict(args)
            result = boundary.submit(operation, **copied)
            return json.dumps(result, sort_keys=True, separators=(",", ":"))

        return _handler

    for operation, schema in TOOL_SCHEMAS.items():
        ctx.register_tool(
            name=operation,
            toolset="kanban",
            schema=schema,
            handler=_make_handler(operation),
            description=schema["description"],
            override=True,
            is_async=False,
        )

_UNRECOGNIZED_OPERATION = "UNRECOGNIZED_OPERATION"
_COMMAND_BOUNDARY_UNAVAILABLE = "COMMAND_BOUNDARY_UNAVAILABLE"
_OPERATION_NOT_IMPLEMENTED = "OPERATION_NOT_IMPLEMENTED"
_COMMAND_EXECUTION_FAILED = "COMMAND_EXECUTION_FAILED"
_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
_IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"

_BOUNDARY_SOURCE = "adrian-kanban"
_BOUNDARY_DESTINATION = "adrian-kanban"

_RECEIPT_TABLE = "adrian_kanban_command_receipts"


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


def _request_digest(
    operation: str,
    target: str,
    expected_version: int,
    payload: dict[str, Any],
    session_id: str,
    workspace_id: str | None,
    execution_context: str,
) -> str:
    identity = {
        "operation": operation,
        "target": target,
        "expected_version": expected_version,
        "payload": payload,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "plugin_version": PLUGIN_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_context": execution_context,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
        idempotency_key = fields.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return self._rejection_internal(
                attempt_id, action, _IDEMPOTENCY_KEY_REQUIRED
            )

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

        request_digest = _request_digest(
            operation=action,
            target=target,
            expected_version=expected_version,
            payload=payload,
            session_id=session_id,
            workspace_id=workspace_id,
            execution_context=execution_context,
        )

        binding = CapabilityBinding(
            operation=action,
            target=target,
            expected_version=expected_version,
            canonical_digest=_canonical_digest(payload),
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
                row = conn.execute(
                    f"SELECT request_digest, response_json FROM {_RECEIPT_TABLE} "
                    "WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    if row["request_digest"] != request_digest:
                        raise _ConflictError()
                    return json.loads(row["response_json"])

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
                try:
                    response_json = json.dumps(
                        {
                            "result": "ACCEPTED",
                            "state_changed": True,
                            "attempt_id": attempt_id,
                            "operation": action,
                            "value": result,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    raise TypeError("handler result is not JSON serializable")
                conn.execute(
                    f"INSERT INTO {_RECEIPT_TABLE} "
                    "(idempotency_key, operation, target, request_digest, "
                    "response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        idempotency_key,
                        action,
                        target,
                        request_digest,
                        response_json,
                        int(time.time()),
                    ),
                )
                return json.loads(response_json)
        except _ConflictError:
            return self._rejection_internal(
                attempt_id, action, _IDEMPOTENCY_CONFLICT
            )
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


class _ConflictError(RuntimeError):
    pass


__all__ = [
    "READ_ONLY_OPERATIONS",
    "ORDINARY_TASK_OPERATIONS",
    "INITIATIVE_OPERATIONS",
    "RECOGNIZED_OPERATIONS",
    "command_boundary",
    "_CommandBoundary",
    "_handle_link",
]

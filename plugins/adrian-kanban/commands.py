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


def _target_for_operation(operation: str, args: dict[str, Any]) -> Any:
    if operation in INITIATIVE_OPERATIONS:
        return args.get("initiative_id")
    if operation == "kanban_link":
        return args.get("child_id")
    if operation in ORDINARY_TASK_OPERATIONS:
        return args.get("task_id")
    return None


class _ModelToolRequestNormalizer:
    def __init__(
        self,
        boundary: Any,
        *,
        board_resolver: Any = None,
    ) -> None:
        self._boundary = boundary
        self._board_resolver = board_resolver

    def submit(
        self,
        operation: str,
        args: dict[str, Any],
        runtime_fields: dict[str, Any],
    ) -> dict[str, Any]:
        attempt_id = None
        try:
            if type(args) is not dict or type(runtime_fields) is not dict:
                raise ValueError("args and runtime_fields must be dicts")

            payload = dict(args)
            runtime = dict(runtime_fields)
            schema = TOOL_SCHEMAS[operation]
            declared = schema["parameters"]["properties"]
            for key in args:
                if key not in declared:
                    raise ValueError(f"undeclared parameter: {key}")

            attempt_id = payload.pop("attempt_id", None)
            idempotency_key = payload.pop("idempotency_key", None)

            if operation in READ_ONLY_OPERATIONS:
                if self._board_resolver is not None:
                    resolved = self._board_resolver(
                        operation,
                        dict(args),
                        dict(runtime_fields),
                    )
                    if type(resolved) is not tuple or len(resolved) != 2:
                        raise ValueError(
                            "board_resolver must return a two-item tuple"
                        )
                    board, _workspace_id = resolved
                    if not isinstance(board, str) or not board.strip():
                        raise ValueError("resolved board must be nonblank")
                    if "board" in payload and payload["board"] != board:
                        raise ValueError("public board mismatch")
                    payload["board"] = board
                return self._boundary.submit(
                    operation,
                    attempt_id=attempt_id,
                    payload=payload,
                )

            if self._board_resolver is None:
                raise ValueError("mutation requires board_resolver")
            session_id = runtime.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("session_id must be nonblank str")

            resolved = self._board_resolver(
                operation,
                dict(args),
                dict(runtime_fields),
            )
            if type(resolved) is not tuple or len(resolved) != 2:
                raise ValueError("board_resolver must return a two-item tuple")
            board, workspace_id = resolved
            if not isinstance(board, str) or not board.strip():
                raise ValueError("resolved board must be nonblank str")
            if workspace_id is not None and (
                not isinstance(workspace_id, str) or not workspace_id.strip()
            ):
                raise ValueError("workspace_id must be None or nonblank str")
            if "board" in payload and payload["board"] != board:
                raise ValueError("public board mismatch")
            payload["board"] = board

            target = _target_for_operation(operation, payload)
            if not isinstance(target, str) or not target.strip():
                raise ValueError("target must be nonblank str")
            return self._boundary.submit(
                operation,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                target=target,
                derive_expected_version=True,
                session_id=session_id.strip(),
                workspace_id=workspace_id,
                execution_context="model-tool",
                payload=payload,
            )
        except Exception:
            return self._boundary._rejection_internal(
                _resolve_attempt_id({"attempt_id": attempt_id}),
                operation,
                _PUBLIC_REQUEST_NORMALIZATION_FAILED,
            )


def _handle_create(context: Any) -> dict[str, Any]:
    payload = context.payload
    allowed_fields = {
        "task_id",
        "initiative_id",
        "title",
        "assignee",
        "body",
        "parents",
        "tenant",
        "priority",
        "workspace_kind",
        "workspace_path",
        "project",
        "goal_mode",
        "goal_max_turns",
        "model",
        "provider",
        "board",
    }
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    initiative_id = payload.get("initiative_id")
    title = payload.get("title")
    assignee = payload.get("assignee")
    board = payload.get("board")

    for field_name, value in (
        ("task_id", task_id),
        ("initiative_id", initiative_id),
        ("title", title),
        ("assignee", assignee),
        ("board", board),
    ):
        if not (type(value) is str and value.strip()):
            raise ValueError(f"{field_name} must be a nonblank string")

    task_id = task_id.strip()
    initiative_id = initiative_id.strip()
    title = title.strip()
    assignee = assignee.strip()
    board = board.strip()

    if "parents" not in payload:
        parents: tuple[str, ...] = ()
    else:
        parents_raw = payload["parents"]
        if parents_raw is None:
            raise ValueError("parents must be a list")
        if type(parents_raw) is not list:
            raise ValueError("parents must be a list")
        seen = set()
        converted = []
        for item in parents_raw:
            if not (type(item) is str and item.strip()):
                raise ValueError("parents must contain nonblank strings")
            item = item.strip()
            if item in seen:
                raise ValueError("parents must not contain duplicates")
            seen.add(item)
            converted.append(item)
        parents = tuple(converted)

    for parent_id in parents:
        parent_row = context.connection.execute(
            "SELECT card_type, task_id, initiative_id, board_slug "
            "FROM adrian_kanban_cards "
            "WHERE task_id = ?",
            (parent_id,),
        ).fetchone()
        if (
            parent_row is None
            or parent_row["card_type"] != "task"
            or parent_row["task_id"] != parent_id
            or parent_row["board_slug"] != board
        ):
            raise ValueError(f"parent task card not found: {parent_id}")
        parent_initiative_id = parent_row["initiative_id"]
        parent_initiative_row = context.connection.execute(
            "SELECT card_type, task_id, board_slug FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND task_id IS NULL",
            (parent_initiative_id,),
        ).fetchone()
        if (
            parent_initiative_row is None
            or parent_initiative_row["card_type"] != "initiative"
            or parent_initiative_row["board_slug"] != board
        ):
            raise ValueError(
                f"parent initiative card not found: {parent_initiative_id}"
            )

    row = context.connection.execute(
        "SELECT card_type, task_id, board_slug FROM adrian_kanban_cards "
        "WHERE initiative_id = ? AND task_id IS NULL",
        (initiative_id,),
    ).fetchone()
    if (
        row is None
        or row["card_type"] != "initiative"
        or row["board_slug"] != board
    ):
        raise ValueError("initiative card not found")

    kwargs: dict[str, Any] = {
        "task_id": task_id,
        "title": title,
        "assignee": assignee,
        "parents": parents,
        "board": board,
    }
    for optional_field in (
        "body",
        "tenant",
        "priority",
        "workspace_kind",
        "workspace_path",
        "project",
        "goal_mode",
        "goal_max_turns",
        "model",
        "provider",
    ):
        if optional_field in payload:
            kwargs[optional_field] = payload[optional_field]

    context.mutation_executor._create_in_active_transaction(
        context.capability,
        context.binding,
        **kwargs,
    )

    context.connection.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type, initiative_id, task_id, title, created_at, "
        "board_slug, record_version) "
        "VALUES ('task', ?, ?, ?, ?, ?, 0)",
        (initiative_id, task_id, title, int(time.time()), board),
    )

    return {"initiative_id": initiative_id, "task_id": task_id}


def _handle_link(context: Any) -> dict[str, Any]:
    parent_id = context.payload.get("parent_id")
    child_id = context.payload.get("child_id")
    board = context.payload.get("board")
    if not (type(parent_id) is str and parent_id.strip()):
        raise ValueError("parent_id must be a nonblank string")
    if not (type(child_id) is str and child_id.strip()):
        raise ValueError("child_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    parent_id = parent_id.strip()
    child_id = child_id.strip()
    board = board.strip()
    if parent_id == child_id:
        raise ValueError("a task cannot link to itself")

    def _validate_endpoint(task_id: str) -> None:
        row = context.connection.execute(
            "SELECT card_type, task_id, initiative_id, board_slug "
            "FROM adrian_kanban_cards "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if (
            row is None
            or row["card_type"] != "task"
            or row["task_id"] != task_id
            or row["board_slug"] != board
        ):
            raise ValueError(f"{task_id} is not a valid task endpoint")
        init_row = context.connection.execute(
            "SELECT card_type, task_id, board_slug FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND task_id IS NULL",
            (row["initiative_id"],),
        ).fetchone()
        if (
            init_row is None
            or init_row["card_type"] != "initiative"
            or init_row["board_slug"] != board
        ):
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
    updated = context.connection.execute(
        "UPDATE adrian_kanban_cards "
        "SET record_version = record_version + 1 "
        "WHERE task_id = ? AND board_slug = ? AND record_version = ?",
        (child_id, board, context.binding.expected_version),
    )
    if updated.rowcount != 1:
        raise ValueError("task version changed during link")
    return {"parent_id": parent_id, "child_id": child_id}


def _load_versioned_task(
    context: Any,
    task_id: Any,
    board: Any,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    if not (type(task_id) is str and task_id.strip()):
        raise ValueError("task_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    task_id = task_id.strip()
    board = board.strip()

    card_rows = context.connection.execute(
        "SELECT card_type, initiative_id, task_id, board_slug, record_version "
        "FROM adrian_kanban_cards WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    if len(card_rows) != 1:
        raise ValueError(f"expected exactly one task card, found {len(card_rows)}")
    card = card_rows[0]
    if (
        card["card_type"] != "task"
        or card["task_id"] != task_id
        or card["board_slug"] != board
    ):
        raise ValueError("task card does not match the resolved board")

    initiative_rows = context.connection.execute(
        "SELECT card_type, task_id, board_slug FROM adrian_kanban_cards "
        "WHERE initiative_id = ? AND task_id IS NULL",
        (card["initiative_id"],),
    ).fetchall()
    if len(initiative_rows) != 1:
        raise ValueError(
            "task must belong to exactly one canonical initiative card"
        )
    initiative = initiative_rows[0]
    if (
        initiative["card_type"] != "initiative"
        or initiative["board_slug"] != board
    ):
        raise ValueError("task initiative does not match the resolved board")
    if card["record_version"] != context.binding.expected_version:
        raise ValueError("task version changed before mutation")

    native_rows = context.connection.execute(
        "SELECT id, current_run_id, claim_lock FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchall()
    if len(native_rows) != 1 or native_rows[0]["id"] != task_id:
        raise ValueError("canonical native task not found")
    return card, native_rows[0]


def _advance_task_version(context: Any, task_id: str, board: str) -> None:
    updated = context.connection.execute(
        "UPDATE adrian_kanban_cards "
        "SET record_version = record_version + 1 "
        "WHERE task_id = ? AND board_slug = ? AND record_version = ?",
        (task_id, board, context.binding.expected_version),
    )
    if updated.rowcount != 1:
        raise ValueError("task version changed during mutation")


def _handle_block(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {"task_id", "reason", "kind", "board"}
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    task_id = payload.get("task_id")
    reason = payload.get("reason")
    board = payload.get("board")
    kind = payload.get("kind")
    for field_name, value in (
        ("task_id", task_id),
        ("reason", reason),
        ("board", board),
    ):
        if not (type(value) is str and value.strip()):
            raise ValueError(f"{field_name} must be a nonblank string")
    if kind is not None and not (type(kind) is str and kind.strip()):
        raise ValueError("kind must be absent or a nonblank string")
    task_id = task_id.strip()
    reason = reason.strip()
    board = board.strip()
    kind = kind.strip() if kind is not None else None

    _card, native = _load_versioned_task(context, task_id, board)
    expected_run_id = native["current_run_id"]
    if expected_run_id is not None and not (
        type(expected_run_id) is int and expected_run_id > 0
    ):
        raise ValueError("native current_run_id must be a positive integer")
    changed = context.mutation_executor._block_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        reason=reason,
        kind=kind,
        expected_run_id=expected_run_id,
    )
    if changed is not True:
        raise ValueError("native task was not blockable")
    _advance_task_version(context, task_id, board)
    return {"task_id": task_id, "blocked": True}


def _handle_unblock(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {"task_id", "board"}
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    task_id = payload.get("task_id")
    board = payload.get("board")
    if not (type(task_id) is str and task_id.strip()):
        raise ValueError("task_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    task_id = task_id.strip()
    board = board.strip()

    _load_versioned_task(context, task_id, board)
    changed = context.mutation_executor._unblock_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
    )
    if changed is not True:
        raise ValueError("native task was not unblockable")
    _advance_task_version(context, task_id, board)
    return {"task_id": task_id, "unblocked": True}


def _handle_comment(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {"task_id", "body", "board"}
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    task_id = payload.get("task_id")
    body = payload.get("body")
    board = payload.get("board")
    for field_name, value in (
        ("task_id", task_id),
        ("body", body),
        ("board", board),
    ):
        if not (type(value) is str and value.strip()):
            raise ValueError(f"{field_name} must be a nonblank string")
    task_id = task_id.strip()
    body = body.strip()
    board = board.strip()
    if "[LIFECYCLE_TRANSITION v1]" in body:
        raise ValueError("generic comments cannot contain lifecycle markers")

    _load_versioned_task(context, task_id, board)
    author = context.binding.session_id
    if not (type(author) is str and author.strip()):
        raise ValueError("binding session_id must be a nonblank string")
    comment_id = context.mutation_executor._comment_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        author=author.strip(),
        body=body,
    )
    if not (type(comment_id) is int and comment_id > 0):
        raise ValueError("native comment id must be a positive integer")
    _advance_task_version(context, task_id, board)
    return {"task_id": task_id, "comment_id": comment_id}


def _handle_heartbeat(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {"task_id", "note", "board"}
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    task_id = payload.get("task_id")
    board = payload.get("board")
    note = payload.get("note")
    if not (type(task_id) is str and task_id.strip()):
        raise ValueError("task_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    if note is not None and type(note) is not str:
        raise ValueError("note must be absent or a string")
    task_id = task_id.strip()
    board = board.strip()
    note = note.strip() if note is not None else None

    _card, native = _load_versioned_task(context, task_id, board)
    claim_lock = native["claim_lock"]
    if not (type(claim_lock) is str and claim_lock.strip()):
        raise ValueError("native task has no active claim")
    expected_run_id = native["current_run_id"]
    if expected_run_id is not None and not (
        type(expected_run_id) is int and expected_run_id > 0
    ):
        raise ValueError("native current_run_id must be a positive integer")
    changed = context.mutation_executor._heartbeat_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        claim_lock=claim_lock.strip(),
        note=note,
        expected_run_id=expected_run_id,
    )
    if changed is not True:
        raise ValueError("native claim could not be extended")
    _advance_task_version(context, task_id, board)
    return {"task_id": task_id, "heartbeat": True}


def register_public_tools(
    ctx: Any,
    boundary: Any,
    *,
    board_resolver: Any = None,
) -> None:
    """Register the 18 ratified public model-tools on the given toolset.

    Each tool is bound to a distinct handler closure that delegates to the
    injected boundary's ``submit`` for its fixed operation. The boundary is
    captured only by construction-time closures; no module-global state is
    stored.
    """

    normalizer = _ModelToolRequestNormalizer(
        boundary,
        board_resolver=board_resolver,
    )

    def _make_handler(operation: str):
        def _handler(args: dict[str, Any], **_runtime_fields: Any) -> str:
            result = normalizer.submit(operation, args, _runtime_fields)
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
_PUBLIC_REQUEST_NORMALIZATION_FAILED = "PUBLIC_REQUEST_NORMALIZATION_FAILED"
_STALE_DERIVED_STATE = "STALE_DERIVED_STATE"

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
    payload: dict[str, Any],
    session_id: str,
    workspace_id: str | None,
    execution_context: str,
    expected_version: int | None = None,
) -> str:
    identity = {
        "operation": operation,
        "target": target,
        "payload": payload,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "plugin_version": PLUGIN_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_context": execution_context,
    }
    if expected_version is not None:
        identity["expected_version"] = expected_version
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
        state_resolver: Any = None,
    ) -> None:
        if type(provider) is not AdrianKanbanAuthorityProvider:
            raise TypeError("provider must be an AdrianKanbanAuthorityProvider")
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("database_path must be a nonblank string")
        if state_resolver is not None and not callable(state_resolver):
            raise TypeError("state_resolver must be callable or None")
        self._database_path = database_path
        self._provider = provider
        self._handlers = MappingProxyType(dict(handlers))
        self._state_resolver = state_resolver

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
        workspace_id = fields.get("workspace_id")
        payload = fields.get("payload")
        derive_expected_version = fields.get("derive_expected_version", False)

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
        if type(derive_expected_version) is not bool:
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
            payload=payload,
            session_id=session_id,
            workspace_id=workspace_id,
            execution_context=execution_context,
            expected_version=(
                None
                if derive_expected_version
                else fields.get("expected_version")
            ),
        )

        try:
            conn = sqlite3.connect(self._database_path)
            conn.row_factory = sqlite3.Row
        except Exception:
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )

        try:
            row = conn.execute(
                f"SELECT request_digest, response_json FROM {_RECEIPT_TABLE} "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != request_digest:
                    raise _ConflictError()
                return json.loads(row["response_json"])

            if derive_expected_version:
                if self._state_resolver is None:
                    raise TypeError("state_resolver is required")
                expected_version = self._state_resolver(
                    conn,
                    action,
                    target,
                    payload,
                )
            else:
                expected_version = fields.get("expected_version")
            if (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 0
            ):
                raise TypeError("expected_version must be a non-negative integer")

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
            capability = self._provider._mint_after_admission(binding)
            adapter = self._provider._create_mutation_executor(conn)

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

                if derive_expected_version:
                    current_version = self._state_resolver(
                        conn,
                        action,
                        target,
                        payload,
                    )
                    if (
                        isinstance(current_version, bool)
                        or not isinstance(current_version, int)
                        or current_version < 0
                        or current_version != expected_version
                    ):
                        raise _StaleDerivedStateError()

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
        except _StaleDerivedStateError:
            return self._rejection_internal(
                attempt_id, action, _STALE_DERIVED_STATE
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


class _StaleDerivedStateError(RuntimeError):
    pass


__all__ = [
    "READ_ONLY_OPERATIONS",
    "ORDINARY_TASK_OPERATIONS",
    "INITIATIVE_OPERATIONS",
    "RECOGNIZED_OPERATIONS",
    "command_boundary",
    "_CommandBoundary",
    "_handle_block",
    "_handle_comment",
    "_handle_create",
    "_handle_heartbeat",
    "_handle_link",
    "_handle_unblock",
]

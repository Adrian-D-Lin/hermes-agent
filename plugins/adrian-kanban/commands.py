from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from types import MappingProxyType, SimpleNamespace
from typing import Any

from .attachments import PreparedAttachment, prepare_url_attachment
from .purge_cleanup import execute_cleanup
from .capability import CapabilityBinding
from .contracts import ContractSnapshot, expand_contract, template_for
from .diagnostics import (
    Boundary,
    CommandRejected,
    DiagnosticCollector,
    FailedCheck,
    NotEvaluatedCheck,
)
from .provider import (
    AdrianKanbanAuthorityProvider,
    PLUGIN_VERSION,
    PROTOCOL_VERSION,
)
from .handoffs import (
    HandoffFinding,
    HandoffValidationRejected,
    canonical_handoff_requirements,
    normalize_handoff_requirements,
    validate_candidate_metadata,
)
from .initiative_mutations import (
    _handle_close_initiative,
    _handle_create_initiative,
    _handle_transition_initiative,
    _handle_update_initiative,
)
from .lifecycle import LifecycleContractRepository
from .output_validators import (
    OutputValidationRejected,
    validate_lifecycle_output,
)
from .projections import attachments_projection, list_projection, show_projection
from .segment_manifest import PreparedSegmentManifest
from .skill_bundle import resolve_skill_binding, skill_contract, validate_skill_bundle
from .task_inputs import (
    PreparedManifest,
    TaskInputPreparationContext,
    finalize_task_input_manifest,
    validate_contract_input_coverage,
)

# Public operation taxonomy. These sets are frozen by the ratified Canon
# operation map and are consumed verbatim by every later S3 slice.
READ_ONLY_OPERATIONS = frozenset({
    "kanban_show",
    "kanban_list",
    "kanban_attachments",
})

ORDINARY_TASK_OPERATIONS = frozenset({
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
})

INITIATIVE_OPERATIONS = frozenset({
    "kanban_create_initiative",
    "kanban_update_initiative",
    "kanban_transition_initiative",
    "kanban_close_initiative",
})

RECOGNIZED_OPERATIONS = (
    READ_ONLY_OPERATIONS | ORDINARY_TASK_OPERATIONS | INITIATIVE_OPERATIONS
)

TOOL_SCHEMAS: dict[str, Any] = {
    "kanban_show": {
        "name": "kanban_show",
        "description": ("Read a single kanban task by its immutable identifier."),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Immutable identifier of the task to show.",
                },
                "initiative_id": {
                    "type": "string",
                    "description": "Immutable identifier of the initiative to show.",
                },
                "board": {
                    "type": "string",
                    "description": "Optional board scope for the read.",
                },
            },
            "oneOf": [
                {"required": ["task_id"]},
                {"required": ["initiative_id"]},
            ],
            "additionalProperties": False,
        },
    },
    "kanban_list": {
        "name": "kanban_list",
        "description": ("List kanban tasks within a scope without mutating state."),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": ("Optional initiative scope to list tasks within."),
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
                    "description": ("Optional flag to include archived tasks."),
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
        "description": ("Read the attachments associated with a kanban task."),
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
        "description": ("Create a new ordinary kanban task at both identity levels."),
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
                "handoff_requirements_v1": {
                    "type": "object",
                    "description": (
                        "Optional immutable lifecycle handoff declaration."
                    ),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Mark an ordinary kanban task as complete."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Block an ordinary kanban task, halting its forward progress."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Attach a comment to an ordinary kanban task."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Link an ordinary kanban task to external references."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Attach a local artifact to an ordinary kanban task."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Attach a remote URL to an ordinary kanban task."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Request changes to an ordinary kanban task."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Request a review of an ordinary kanban task."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Create a new kanban initiative."),
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Update an existing kanban initiative."),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "Immutable identifier of the initiative to update.",
                },
                "update_kind": {
                    "type": "string",
                    "description": (
                        "Kind of update to apply to the initiative. One of "
                        "body_update, phase_result, segment_manifest_projection, "
                        "orchestration_checkpoint, or purge_replace_task."
                    ),
                    "enum": [
                        "body_update",
                        "phase_result",
                        "segment_manifest_projection",
                        "orchestration_checkpoint",
                        "purge_replace_task",
                    ],
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
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Transition an existing kanban initiative to a new phase."),
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
                "to_segment_id": {
                    "type": "string",
                    "description": "Optional target segment identifier.",
                },
                "reconciliation_ref": {
                    "type": "string",
                    "description": ("Reconciliation reference backing the transition."),
                },
                "approval_id": {
                    "type": "string",
                    "description": "Approval reference authorizing the transition.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
        "description": ("Close an existing kanban initiative."),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "Immutable identifier of the initiative to close.",
                },
                "closure_result_ref": {
                    "type": "string",
                    "description": ("Reference describing the closure outcome."),
                },
                "approval_id": {
                    "type": "string",
                    "description": "Approval reference authorizing closure.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": ("Caller-supplied key guaranteeing safe replay."),
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
                    if type(resolved) is not tuple or len(resolved) != 3:
                        raise ValueError(
                            "board_resolver must return a three-item tuple"
                        )
                    board, _workspace_id, _actor_profile = resolved
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
            if type(resolved) is not tuple or len(resolved) != 3:
                raise ValueError("board_resolver must return a three-item tuple")
            board, workspace_id, actor_profile = resolved
            if not isinstance(board, str) or not board.strip():
                raise ValueError("resolved board must be nonblank str")
            if workspace_id is not None and (
                not isinstance(workspace_id, str) or not workspace_id.strip()
            ):
                raise ValueError("workspace_id must be None or nonblank str")
            if not (type(actor_profile) is str and actor_profile.strip()):
                raise ValueError("actor_profile must be a nonblank str")
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
                actor_profile=actor_profile.strip(),
                payload=payload,
            )
        except Exception:
            return self._boundary._rejection_internal(
                _resolve_attempt_id({"attempt_id": attempt_id}),
                operation,
                _PUBLIC_REQUEST_NORMALIZATION_FAILED,
            )


def _handle_show(context: Any) -> dict[str, Any]:
    payload = context.payload
    allowed_fields = {"task_id", "initiative_id", "board"}
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    initiative_id = payload.get("initiative_id")
    board = payload.get("board")

    if not isinstance(board, str) or not board.strip():
        raise ValueError("board must be nonblank str")

    has_task = isinstance(task_id, str) and task_id.strip()
    has_initiative = isinstance(initiative_id, str) and initiative_id.strip()

    if has_task and has_initiative:
        raise ValueError("provide exactly one of task_id or initiative_id")
    if not has_task and not has_initiative:
        raise ValueError("provide exactly one of task_id or initiative_id")

    if has_task:
        return show_projection(context.connection, task_id.strip(), board.strip())
    return show_projection(
        context.connection,
        None,
        board.strip(),
        initiative_id.strip(),
    )


def _handle_list(context: Any) -> dict[str, Any]:
    payload = context.payload
    allowed_fields = {
        "initiative_id", "assignee", "status", "tenant",
        "include_archived", "limit", "board",
    }
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    for field in ("initiative_id", "assignee", "status", "tenant", "board"):
        val = payload.get(field)
        if val is not None and (not isinstance(val, str) or not val.strip()):
            raise ValueError(f"{field} must be nonblank str or null")

    include_archived = payload.get("include_archived")
    if include_archived is not None and not isinstance(include_archived, bool):
        raise ValueError("include_archived must be bool")

    limit = payload.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 500:
            raise ValueError("limit must be int 1-500")

    return list_projection(
        context.connection,
        board=payload.get("board"),
        initiative_id=payload.get("initiative_id"),
        assignee=payload.get("assignee"),
        status=payload.get("status"),
        tenant=payload.get("tenant"),
        include_archived=include_archived,
        limit=limit,
    )


def _handle_attachments(context: Any) -> dict[str, Any]:
    payload = context.payload
    allowed_fields = {"task_id", "board"}
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    board = payload.get("board")

    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be nonblank str")
    if not isinstance(board, str) or not board.strip():
        raise ValueError("board must be nonblank str")

    return attachments_projection(
        context.connection,
        task_id.strip(),
        board.strip(),
    )


def _handle_attach(context: Any) -> dict[str, Any]:
    payload = context.payload
    allowed_fields = {
        "task_id",
        "filename",
        "content_base64",
        "content_type",
        "board",
    }
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    filename = payload.get("filename")
    content_base64 = payload.get("content_base64")
    content_type = payload.get("content_type")
    board = payload.get("board")

    for field_name, value in (
        ("task_id", task_id),
        ("filename", filename),
        ("content_base64", content_base64),
    ):
        if type(value) is not str or not value.strip():
            raise ValueError(f"{field_name} must be a nonblank string")

    if content_type is not None and (
        type(content_type) is not str or not content_type.strip()
    ):
        raise ValueError("content_type must be None or a nonblank string")

    if type(board) is not str or not board.strip():
        raise ValueError("board must be a nonblank string")

    task_id = task_id.strip()
    filename = filename.strip()
    content_base64 = content_base64.strip()
    board = board.strip()
    if content_type is not None:
        content_type = content_type.strip()

    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("content_base64 must be valid base64") from exc

    _load_versioned_task(context, task_id, board)
    attachment_id = context.mutation_executor._attach_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        filename=filename,
        data=data,
        content_type=content_type,
        board=board,
    )
    if type(attachment_id) is not int or attachment_id <= 0:
        raise ValueError("invalid attachment id")
    _advance_task_version(context, task_id, board)
    return {
        "task_id": task_id,
        "attachment_id": attachment_id,
        "filename": filename,
        "size": len(data),
    }


def _validate_attach_url_payload(
    payload: dict[str, Any],
) -> tuple[str, str, str | None, str | None, str]:
    allowed_fields = {"task_id", "url", "filename", "content_type", "board"}
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    url = payload.get("url")
    filename = payload.get("filename")
    content_type = payload.get("content_type")
    board = payload.get("board")
    for field_name, value in (
        ("task_id", task_id),
        ("url", url),
        ("board", board),
    ):
        if type(value) is not str or not value.strip():
            raise ValueError(f"{field_name} must be a nonblank string")
    for field_name, value in (
        ("filename", filename),
        ("content_type", content_type),
    ):
        if value is not None and (type(value) is not str or not value.strip()):
            raise ValueError(f"{field_name} must be None or a nonblank string")
    return (
        task_id.strip(),
        url.strip(),
        filename.strip() if filename is not None else None,
        content_type.strip() if content_type is not None else None,
        board.strip(),
    )


def _prepare_attach_url_payload(payload: dict[str, Any]) -> PreparedAttachment:
    _, url, filename, content_type, _ = _validate_attach_url_payload(payload)
    return prepare_url_attachment(
        url,
        filename=filename,
        content_type=content_type,
    )


def _handle_attach_url(context: Any) -> dict[str, Any]:
    task_id, _, _, _, board = _validate_attach_url_payload(context.payload)
    if type(context.prepared_attachment) is not PreparedAttachment:
        raise ValueError("prepared_attachment must be a PreparedAttachment")
    prepared = context.prepared_attachment

    _load_versioned_task(context, task_id, board)
    attachment_id = context.mutation_executor._attach_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        filename=prepared.filename,
        data=prepared.data,
        content_type=prepared.content_type,
        board=board,
    )
    if type(attachment_id) is not int or attachment_id <= 0:
        raise ValueError("invalid attachment id")
    row = context.connection.execute(
        "SELECT filename FROM task_attachments WHERE id = ? AND task_id = ?",
        (attachment_id, task_id),
    ).fetchone()
    if row is None:
        raise ValueError("attachment metadata not found")
    _advance_task_version(context, task_id, board)
    return {
        "task_id": task_id,
        "attachment_id": attachment_id,
        "filename": row["filename"],
        "size": len(prepared.data),
    }


def _validate_lifecycle_predecessor(
    connection: sqlite3.Connection,
    initiative_card_id: int,
    lifecycle_snapshot: ContractSnapshot,
) -> None:
    predecessor_ref = lifecycle_snapshot.predecessor_ref
    if predecessor_ref is None:
        raise ValueError("predecessor_ref is required")
    if lifecycle_snapshot.sequence_ordinal is None:
        raise ValueError("sequence is required")
    template = template_for(lifecycle_snapshot.step)
    if template.sequence is None or template.sequence.predecessor_step is None:
        raise ValueError("expected predecessor_step is required")
    expected_step = template.sequence.predecessor_step
    release_condition = lifecycle_snapshot.release_condition
    if release_condition == "accepted_completion":
        row = connection.execute(
            "SELECT 1 FROM task_reviewer_verdicts v "
            "JOIN task_candidate_handoffs h ON h.candidate_id = v.candidate_id "
            "AND h.task_card_id = v.task_card_id AND h.task_id = v.task_id "
            "JOIN task_lifecycle_contracts c ON c.task_card_id = h.task_card_id "
            "AND c.task_id = h.task_id "
            "WHERE h.candidate_id = ? "
            "AND h.task_card_id = c.task_card_id "
            "AND h.task_id = c.task_id "
            "AND c.initiative_card_id = ? "
            "AND c.initiative_id = ? "
            "AND c.segment_id IS ? "
            "AND c.step = ? "
            "AND v.verdict = 'accepted'",
            (
                predecessor_ref,
                initiative_card_id,
                lifecycle_snapshot.initiative_id,
                lifecycle_snapshot.segment_id,
                expected_step,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("accepted_completion predecessor not found")
    elif release_condition == "initiative_checkpoint":
        row = connection.execute(
            "SELECT canonical_payload FROM initiative_phase_results "
            "WHERE result_id = ? "
            "AND initiative_card_id = ? "
            "AND initiative_id = ? "
            "AND phase = ? "
            "AND segment_id IS ? "
            "AND accepted = 1",
            (
                predecessor_ref,
                initiative_card_id,
                lifecycle_snapshot.initiative_id,
                lifecycle_snapshot.phase,
                lifecycle_snapshot.segment_id,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("initiative_checkpoint predecessor not found")
        try:
            payload = json.loads(row["canonical_payload"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("initiative_checkpoint payload is not valid JSON") from exc
        if type(payload) is not dict or payload.get("step") != expected_step:
            raise ValueError("initiative_checkpoint step mismatch")
    else:
        raise ValueError("unknown release condition")


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
        "handoff_requirements_v1",
        "model",
        "provider",
        "board",
        "lifecycle_contract_v1",
        "task_input_manifest_v1",
    }
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    initiative_id = payload.get("initiative_id")
    title = payload.get("title")
    assignee = payload.get("assignee")
    board = payload.get("board")
    raw_handoff_requirements = payload.get("handoff_requirements_v1")
    raw_lifecycle_contract = payload.get("lifecycle_contract_v1")

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

    if context.connection.execute(
        "SELECT 1 FROM task_purge_replacements WHERE predecessor_task_id = ?",
        (task_id,),
    ).fetchone() is not None:
        raise ValueError("task_id already retired as a purge predecessor")

    handoff_requirements = None

    if raw_handoff_requirements is not None:
        if payload.get("goal_mode") is not True:
            raise ValueError("handoff-governed tasks require goal_mode to be true")
        handoff_requirements = normalize_handoff_requirements(
            raw_handoff_requirements,
            context.known_profiles,
        )
        if assignee not in context.known_profiles:
            raise ValueError("assignee must be a known execution profile")

    lifecycle_snapshot = None
    skill_binding = None
    if raw_lifecycle_contract is not None:
        if type(raw_lifecycle_contract) is not dict:
            raise ValueError("lifecycle_contract_v1 must be a dict")

        required_keys = {
            "version",
            "step",
            "baseline_refs",
            "governing_source_refs",
            "prior_record_refs",
        }
        optional_keys = {"segment_id", "segment_workspace_id", "predecessor_ref"}
        allowed_keys = required_keys | optional_keys

        if set(raw_lifecycle_contract.keys()) - allowed_keys:
            raise ValueError("lifecycle_contract_v1 has unknown keys")
        if not required_keys.issubset(raw_lifecycle_contract.keys()):
            raise ValueError("lifecycle_contract_v1 missing required keys")

        version = raw_lifecycle_contract["version"]
        if type(version) is not int or isinstance(version, bool) or version != 1:
            raise ValueError("lifecycle_contract_v1 version must be int 1")

        step = raw_lifecycle_contract["step"]
        if type(step) is not str or not step.strip() or step != step.strip():
            raise ValueError(
                "lifecycle_contract_v1 step must be a nonblank string without "
                "surrounding whitespace"
            )

        def _validate_ref_group(name: str, value: Any) -> tuple[str, ...]:
            if type(value) is not list:
                raise ValueError(f"lifecycle_contract_v1 {name} must be a list")
            seen = set()
            for item in value:
                if (
                    type(item) is not str
                    or not item.strip()
                    or item != item.strip()
                ):
                    raise ValueError(
                        f"lifecycle_contract_v1 {name} items must be nonblank "
                        "strings without surrounding whitespace"
                    )
                if item in seen:
                    raise ValueError(
                        f"lifecycle_contract_v1 {name} must contain unique items"
                    )
                seen.add(item)
            return tuple(value)

        baseline_refs = _validate_ref_group(
            "baseline_refs", raw_lifecycle_contract["baseline_refs"]
        )
        governing_source_refs = _validate_ref_group(
            "governing_source_refs", raw_lifecycle_contract["governing_source_refs"]
        )
        prior_record_refs = _validate_ref_group(
            "prior_record_refs", raw_lifecycle_contract["prior_record_refs"]
        )

        segment_id = raw_lifecycle_contract.get("segment_id")
        segment_workspace_id = raw_lifecycle_contract.get("segment_workspace_id")
        predecessor_ref = raw_lifecycle_contract.get("predecessor_ref")

        for opt_name, opt_val in (
            ("segment_id", segment_id),
            ("segment_workspace_id", segment_workspace_id),
            ("predecessor_ref", predecessor_ref),
        ):
            if opt_val is not None and (
                type(opt_val) is not str
                or not opt_val.strip()
                or opt_val != opt_val.strip()
            ):
                raise ValueError(
                    f"lifecycle_contract_v1 {opt_name} must be None or a "
                    "nonblank string without surrounding whitespace"
                )

        if (segment_id is None) != (segment_workspace_id is None):
            raise ValueError(
                "lifecycle_contract_v1 segment_id and segment_workspace_id must "
                "both be set or both null"
            )

        try:
            lifecycle_snapshot = expand_contract(
                step=step,
                initiative_id=initiative_id,
                baseline_refs=baseline_refs,
                governing_source_refs=governing_source_refs,
                prior_record_refs=prior_record_refs,
                segment_id=segment_id,
                segment_workspace_id=segment_workspace_id,
                predecessor_ref=predecessor_ref,
            )
        except Exception as exc:
            raise ValueError(f"lifecycle contract expansion failed: {exc}") from exc

        if payload.get("goal_mode") is not True:
            raise ValueError("lifecycle-governed tasks require goal_mode to be true")
        if handoff_requirements is None:
            raise ValueError(
                "lifecycle-governed tasks require handoff_requirements_v1"
            )
        if type(context.prepared_manifest) is not PreparedManifest:
            raise ValueError("lifecycle-governed tasks require a prepared manifest")

        try:
            validate_contract_input_coverage(
                lifecycle_snapshot, context.prepared_manifest
            )
        except ValueError as exc:
            raise ValueError(f"lifecycle reference coverage failed: {exc}") from exc

        if not validate_skill_bundle():
            raise ValueError("skill bundle validation failed")

        skill_binding = resolve_skill_binding(lifecycle_snapshot.phase)
        expected_contract_id, expected_contract_version = skill_contract(
            lifecycle_snapshot.phase
        )
        if lifecycle_snapshot.contract_id != expected_contract_id:
            raise ValueError("skill contract ID mismatch")
        if str(lifecycle_snapshot.contract_version) != expected_contract_version:
            raise ValueError("skill contract version mismatch")

        body = payload.get("body")
        if type(body) is not str:
            raise ValueError("lifecycle-governed tasks require a string body")

        required_lines = [f"initiative_id: {initiative_id}", f"step: {step}"]
        if segment_id is not None:
            required_lines.append(f"segment_id: {segment_id}")

        body_lines = set(body.splitlines())
        for line in required_lines:
            if line not in body_lines:
                raise ValueError(f"body must contain exact line: {line}")

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
        "SELECT id, card_type, task_id, board_slug FROM adrian_kanban_cards "
        "WHERE initiative_id = ? AND task_id IS NULL",
        (initiative_id,),
    ).fetchone()
    if row is None or row["card_type"] != "initiative" or row["board_slug"] != board:
        raise ValueError("initiative card not found")
    initiative_card_id = row["id"]

    if lifecycle_snapshot is not None:
        if assignee != lifecycle_snapshot.execution_profile:
            raise ValueError("assignee does not match lifecycle execution profile")

        transition_row = context.connection.execute(
            "SELECT to_phase, to_segment_id FROM initiative_transitions "
            "WHERE initiative_card_id = ? ORDER BY transition_id DESC LIMIT 1",
            (initiative_card_id,),
        ).fetchone()
        if transition_row is None:
            raise ValueError("initiative has no transitions")
        if transition_row["to_phase"] != lifecycle_snapshot.phase:
            raise ValueError("initiative phase does not match lifecycle snapshot")
        if transition_row["to_segment_id"] != lifecycle_snapshot.segment_id:
            raise ValueError("initiative segment does not match lifecycle snapshot")

        if lifecycle_snapshot.segment_id is not None:
            workspace_row = context.connection.execute(
                "SELECT workspace_id, initiative_card_id, initiative_id, "
                "segment_id, active, lifecycle_state FROM segment_workspaces "
                "WHERE workspace_id = ?",
                (lifecycle_snapshot.segment_workspace_id,),
            ).fetchone()
            if workspace_row is None:
                raise ValueError("segment workspace not found")
            if workspace_row["initiative_card_id"] != initiative_card_id:
                raise ValueError("segment workspace initiative card mismatch")
            if workspace_row["initiative_id"] != initiative_id:
                raise ValueError("segment workspace initiative mismatch")
            if workspace_row["segment_id"] != lifecycle_snapshot.segment_id:
                raise ValueError("segment workspace segment mismatch")
            if workspace_row["active"] != 1:
                raise ValueError("segment workspace is not active")
            if workspace_row["lifecycle_state"] != "active":
                raise ValueError("segment workspace lifecycle state is not active")

        if lifecycle_snapshot.predecessor_ref is not None:
            _validate_lifecycle_predecessor(
                context.connection, initiative_card_id, lifecycle_snapshot
            )

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

    inserted = context.connection.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type, initiative_id, task_id, title, created_at, "
        "board_slug, record_version) "
        "VALUES ('task', ?, ?, ?, ?, ?, 0)",
        (initiative_id, task_id, title, int(time.time()), board),
    )
    task_card_id = int(inserted.lastrowid or 0)
    if task_card_id <= 0:
        raise ValueError("unified task card identity was not created")

    if handoff_requirements is not None:
        context.connection.execute(
            "INSERT INTO task_handoff_requirements "
            "(task_card_id, task_id, version, execution_profile, reviewer, "
            "canonical_payload, created_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
            (
                task_card_id,
                task_id,
                assignee,
                handoff_requirements["reviewer"],
                canonical_handoff_requirements(handoff_requirements),
                int(time.time()),
            ),
        )

    if context.prepared_manifest is not None:
        snapshot_attachment_ids = {}
        for entry in context.prepared_manifest.entries:
            if entry.source_kind == "snapshot_attachment":
                if (
                    entry.snapshot_bytes is None
                    or entry.filename is None
                    or entry.content_type is None
                ):
                    raise ValueError("snapshot entry missing required fields")
                attachment_id = (
                    context.mutation_executor._attach_during_create_in_active_transaction(
                        context.capability,
                        context.binding,
                        task_id=task_id,
                        filename=entry.filename,
                        data=entry.snapshot_bytes,
                        content_type=entry.content_type,
                        board=board,
                    )
                )
                if type(attachment_id) is not int or attachment_id <= 0:
                    raise ValueError("invalid attachment id")
                snapshot_attachment_ids[entry.workspace_path] = attachment_id

        finalized_manifest = finalize_task_input_manifest(
            context.prepared_manifest,
            snapshot_attachment_ids,
        )

        context.connection.execute(
            "INSERT INTO task_input_manifests "
            "(task_card_id, task_id, canonical_payload, "
            "declared_inputs_accessible, created_at) VALUES (?, ?, ?, 1, ?)",
            (
                task_card_id,
                task_id,
                finalized_manifest.canonical_payload,
                int(time.time()),
            ),
        )

        for entry in finalized_manifest.entries:
            context.connection.execute(
                "INSERT INTO task_input_entries "
                "(task_card_id, task_id, workspace_path, sha256, source_kind, "
                "source_locator, context_guidance) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_card_id,
                    task_id,
                    entry.workspace_path,
                    entry.sha256,
                    entry.source_kind,
                    entry.source_locator,
                    entry.context_guidance,
                ),
            )

    if lifecycle_snapshot is not None:
        repo = LifecycleContractRepository(context.connection)
        repo.attach(
            task_id=task_id,
            snapshot=lifecycle_snapshot,
            skill=skill_binding,
            created_at=int(time.time()),
        )

    return {
        "initiative_id": initiative_id,
        "task_id": task_id,
        "handoff_governed": handoff_requirements is not None,
        **(
            {
                "lifecycle_governed": True,
                "contract_id": lifecycle_snapshot.contract_id,
                "contract_version": lifecycle_snapshot.contract_version,
                "step": lifecycle_snapshot.step,
            }
            if lifecycle_snapshot is not None
            else {}
        ),
    }


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
        "SELECT id, card_type, initiative_id, task_id, board_slug, record_version "
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
        raise ValueError("task must belong to exactly one canonical initiative card")
    initiative = initiative_rows[0]
    if initiative["card_type"] != "initiative" or initiative["board_slug"] != board:
        raise ValueError("task initiative does not match the resolved board")
    if card["record_version"] != context.binding.expected_version:
        raise ValueError("task version changed before mutation")

    native_rows = context.connection.execute(
        "SELECT id, status, assignee, current_run_id, claim_lock "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchall()
    if len(native_rows) != 1 or native_rows[0]["id"] != task_id:
        raise ValueError("canonical native task not found")
    return card, native_rows[0]


def _load_validated_handoff_requirement(
    context: Any,
    task_card_id: int,
    task_id: str,
) -> SimpleNamespace | None:
    if not (type(task_card_id) is int and task_card_id > 0):
        raise ValueError("task_card_id must be a positive integer")
    if not (type(task_id) is str and task_id.strip()):
        raise ValueError("task_id must be a nonblank string")

    handoff_rows = context.connection.execute(
        "SELECT task_card_id, task_id, version, execution_profile, reviewer, "
        "canonical_payload FROM task_handoff_requirements "
        "WHERE task_card_id = ? AND task_id = ?",
        (task_card_id, task_id),
    ).fetchall()
    if len(handoff_rows) > 1:
        raise ValueError("corrupt handoff requirements: multiple rows found")
    if not handoff_rows:
        return None

    handoff = handoff_rows[0]
    if not (type(handoff["version"]) is int and handoff["version"] == 1):
        raise ValueError("handoff version must be 1")

    execution_profile = handoff["execution_profile"]
    if not (
        type(execution_profile) is str
        and execution_profile
        and execution_profile == execution_profile.strip()
    ):
        raise ValueError("handoff execution_profile must be a nonblank string")
    if execution_profile not in context.known_profiles:
        raise ValueError("handoff execution_profile is not a known profile")

    fixed_reviewer = handoff["reviewer"]
    if not (
        type(fixed_reviewer) is str
        and fixed_reviewer
        and fixed_reviewer == fixed_reviewer.strip()
    ):
        raise ValueError("handoff reviewer must be a nonblank string")
    if fixed_reviewer not in context.known_profiles:
        raise ValueError("handoff reviewer is not a known profile")

    stored_payload = handoff["canonical_payload"]
    if not (
        type(stored_payload) is str
        and stored_payload
        and stored_payload == stored_payload.strip()
    ):
        raise ValueError("handoff canonical_payload must be a nonblank string")
    try:
        parsed_payload = json.loads(stored_payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("handoff canonical_payload is not valid JSON") from exc
    if type(parsed_payload) is not dict:
        raise ValueError("handoff canonical_payload must be a dict")
    try:
        normalized = normalize_handoff_requirements(
            parsed_payload,
            context.known_profiles,
        )
    except HandoffValidationRejected as exc:
        raise ValueError("handoff canonical_payload failed normalization") from exc
    if canonical_handoff_requirements(normalized) != stored_payload:
        raise ValueError("handoff canonical_payload does not match normalized form")
    if fixed_reviewer != normalized["reviewer"]:
        raise ValueError("handoff reviewer does not match normalized reviewer")

    return SimpleNamespace(
        execution_profile=execution_profile,
        reviewer=fixed_reviewer,
        normalized=normalized,
    )


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


def _handle_complete(context: Any) -> dict[str, Any]:
    payload = context.payload
    allowed_fields = {
        "task_id",
        "summary",
        "metadata",
        "result",
        "created_cards",
        "artifacts",
        "board",
    }
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    summary = payload.get("summary")
    metadata = payload.get("metadata")
    result = payload.get("result")
    created_cards = payload.get("created_cards")
    artifacts = payload.get("artifacts")
    board = payload.get("board")

    if not (type(task_id) is str and task_id.strip()):
        raise ValueError("task_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    if summary is not None and type(summary) is not str:
        raise ValueError("summary must be absent or a string")
    if result is not None and type(result) is not str:
        raise ValueError("result must be absent or a string")
    if metadata is not None and type(metadata) is not dict:
        raise ValueError("metadata must be absent or a dict")
    if created_cards is not None:
        if type(created_cards) is not list:
            raise ValueError("created_cards must be absent or a list")
        seen = set()
        for item in created_cards:
            if not (type(item) is str and item.strip()):
                raise ValueError("created_cards items must be nonblank strings")
            trimmed = item.strip()
            if trimmed in seen:
                raise ValueError("created_cards must contain unique items")
            seen.add(trimmed)
    if artifacts is not None:
        if type(artifacts) is not list:
            raise ValueError("artifacts must be absent or a list")
        seen = set()
        for item in artifacts:
            if not (type(item) is str and item.strip()):
                raise ValueError("artifacts items must be nonblank strings")
            trimmed = item.strip()
            if trimmed in seen:
                raise ValueError("artifacts must contain unique items")
            seen.add(trimmed)

    task_id = task_id.strip()
    board = board.strip()
    if created_cards is not None:
        created_cards = tuple(item.strip() for item in created_cards)
    if artifacts is not None:
        artifacts = [item.strip() for item in artifacts]

    if metadata is not None:
        metadata = dict(metadata)
    elif artifacts is not None:
        metadata = {}
    if artifacts is not None:
        if "artifacts" in metadata:
            if metadata["artifacts"] != artifacts:
                raise ValueError("artifacts conflict with metadata")
        else:
            metadata["artifacts"] = artifacts

    card, native = _load_versioned_task(context, task_id, board)
    card_id = card["id"]
    if not (type(card_id) is int and card_id > 0):
        raise ValueError("card id must be a positive integer")
    handoff = _load_validated_handoff_requirement(context, card_id, task_id)

    if handoff is None:
        expected_run_id = native["current_run_id"]
        if expected_run_id is not None and not (
            type(expected_run_id) is int and expected_run_id > 0
        ):
            raise ValueError("native current_run_id must be a positive integer")
        changed = context.mutation_executor._complete_in_active_transaction(
            context.capability,
            context.binding,
            task_id=task_id,
            result=result,
            summary=summary,
            metadata=metadata,
            created_cards=created_cards,
            expected_run_id=expected_run_id,
        )
        if changed is not True:
            raise ValueError("native task was not completable")
        _advance_task_version(context, task_id, board)
        return {"task_id": task_id, "accepted_candidate_id": None}

    execution_profile = handoff.execution_profile
    fixed_reviewer = handoff.reviewer
    if native["status"] != "running":
        raise ValueError("task must be in running state for governed completion")
    current_run_id = native["current_run_id"]
    if not (type(current_run_id) is int and current_run_id > 0):
        raise ValueError("task must have a positive current_run_id")
    if not (type(native["claim_lock"]) is str and native["claim_lock"].strip()):
        raise ValueError("task must have a nonblank claim_lock")
    if native["assignee"] != fixed_reviewer:
        raise ValueError("task assignee does not match fixed reviewer")
    if context.binding.actor_profile != fixed_reviewer:
        raise ValueError("actor_profile does not match fixed reviewer")

    run_row = context.connection.execute(
        "SELECT status, ended_at, profile FROM task_runs WHERE id = ? AND task_id = ?",
        (current_run_id, task_id),
    ).fetchone()
    if run_row is None:
        raise ValueError("execution run not found")
    if run_row["status"] != "running":
        raise ValueError("execution run must be in running state")
    if run_row["ended_at"] is not None:
        raise ValueError("execution run must not be ended")
    if run_row["profile"] != fixed_reviewer:
        raise ValueError("execution run profile does not match fixed reviewer")

    claimed_event = context.connection.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
        "ORDER BY id DESC LIMIT 1",
        (task_id, current_run_id),
    ).fetchone()
    if claimed_event is None:
        raise ValueError("claimed event not found")
    try:
        claimed_payload = json.loads(claimed_event["payload"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("claimed event payload is not valid JSON") from exc
    if type(claimed_payload) is not dict:
        raise ValueError("claimed event payload must be a dict")
    if claimed_payload.get("source_status") != "review":
        raise ValueError("active run was not claimed from review")

    candidate_row = context.connection.execute(
        "SELECT candidate_id, task_card_id, task_id, execution_run_id, "
        "reviewer, submitted_by FROM task_candidate_handoffs "
        "WHERE task_card_id = ? AND task_id = ? "
        "ORDER BY execution_run_id DESC LIMIT 1",
        (card_id, task_id),
    ).fetchone()
    if candidate_row is None:
        raise ValueError("no structurally admitted candidate found")
    if candidate_row["task_card_id"] != card_id:
        raise ValueError("candidate task_card_id mismatch")
    if candidate_row["task_id"] != task_id:
        raise ValueError("candidate task_id mismatch")
    if candidate_row["reviewer"] != fixed_reviewer:
        raise ValueError("candidate reviewer mismatch")
    if candidate_row["submitted_by"] != execution_profile:
        raise ValueError("candidate submitted_by does not match execution profile")

    candidate_run = context.connection.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE id = ? AND task_id = ?",
        (candidate_row["execution_run_id"], task_id),
    ).fetchone()
    if candidate_run is None:
        raise ValueError("candidate execution run not found")
    if candidate_run["outcome"] != "review_requested":
        raise ValueError("candidate execution run outcome must be review_requested")
    if not (type(candidate_run["ended_at"]) is int and candidate_run["ended_at"] > 0):
        raise ValueError("candidate execution run must have a positive ended_at")

    verdict_row = context.connection.execute(
        "SELECT 1 FROM task_reviewer_verdicts WHERE candidate_id = ?",
        (candidate_row["candidate_id"],),
    ).fetchone()
    if verdict_row is not None:
        raise ValueError("candidate already has a reviewer verdict")

    changed = context.mutation_executor._complete_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        result=result,
        summary=summary,
        metadata=metadata,
        created_cards=created_cards,
        expected_run_id=current_run_id,
    )
    if changed is not True:
        raise ValueError("native task was not completable")

    ended_run = context.connection.execute(
        "SELECT outcome, ended_at, summary FROM task_runs WHERE id = ? AND task_id = ?",
        (current_run_id, task_id),
    ).fetchone()
    if ended_run is None:
        raise ValueError("execution run not found after mutation")
    if ended_run["outcome"] != "completed":
        raise ValueError("execution run outcome must be completed")
    if not (type(ended_run["ended_at"]) is int and ended_run["ended_at"] > 0):
        raise ValueError("execution run must have a positive ended_at")
    if ended_run["summary"] is not None and type(ended_run["summary"]) is not str:
        raise ValueError("execution run summary must be a string or null")

    context.connection.execute(
        "INSERT INTO task_reviewer_verdicts "
        "(verdict_id, task_card_id, task_id, candidate_id, review_run_id, "
        "reviewer, verdict, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            card_id,
            task_id,
            candidate_row["candidate_id"],
            current_run_id,
            fixed_reviewer,
            "accepted",
            ended_run["summary"],
            int(time.time()),
        ),
    )
    _advance_task_version(context, task_id, board)
    return {
        "task_id": task_id,
        "accepted_candidate_id": candidate_row["candidate_id"],
    }


def _handle_request_changes(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {"task_id", "reason", "board"}
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    task_id = payload.get("task_id")
    reason = payload.get("reason")
    board = payload.get("board")
    for field_name, value in (
        ("task_id", task_id),
        ("reason", reason),
        ("board", board),
    ):
        if not (type(value) is str and value.strip()):
            raise ValueError(f"{field_name} must be a nonblank string")
    task_id = task_id.strip()
    reason = reason.strip()
    board = board.strip()

    card, native = _load_versioned_task(context, task_id, board)
    card_id = card["id"]
    if not (type(card_id) is int and card_id > 0):
        raise ValueError("card id must be a positive integer")
    handoff = _load_validated_handoff_requirement(context, card_id, task_id)

    current_run_id = native["current_run_id"]
    if not (type(current_run_id) is int and current_run_id > 0):
        raise ValueError("native current_run_id must be a positive integer")

    if handoff is not None:
        execution_profile = handoff.execution_profile
        fixed_reviewer = handoff.reviewer
        if native["status"] != "running":
            raise ValueError("task must be in running state for governed changes")
        if not (type(native["claim_lock"]) is str and native["claim_lock"].strip()):
            raise ValueError("task must have a nonblank claim_lock")
        if native["assignee"] != fixed_reviewer:
            raise ValueError("task assignee does not match fixed reviewer")
        if context.binding.actor_profile != fixed_reviewer:
            raise ValueError("actor_profile does not match fixed reviewer")

        run_row = context.connection.execute(
            "SELECT status, ended_at, profile FROM task_runs "
            "WHERE id = ? AND task_id = ?",
            (current_run_id, task_id),
        ).fetchone()
        if run_row is None:
            raise ValueError("execution run not found")
        if run_row["status"] != "running":
            raise ValueError("execution run must be in running state")
        if run_row["ended_at"] is not None:
            raise ValueError("execution run must not be ended")
        if run_row["profile"] != fixed_reviewer:
            raise ValueError("execution run profile does not match fixed reviewer")

        claimed_event = context.connection.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
            "ORDER BY id DESC LIMIT 1",
            (task_id, current_run_id),
        ).fetchone()
        if claimed_event is None:
            raise ValueError("claimed event not found")
        try:
            claimed_payload = json.loads(claimed_event["payload"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("claimed event payload is not valid JSON") from exc
        if type(claimed_payload) is not dict:
            raise ValueError("claimed event payload must be a dict")
        if claimed_payload.get("source_status") != "review":
            raise ValueError("active run was not claimed from review")

        candidate_row = context.connection.execute(
            "SELECT candidate_id, task_card_id, task_id, execution_run_id, "
            "reviewer, submitted_by FROM task_candidate_handoffs "
            "WHERE task_card_id = ? AND task_id = ? "
            "ORDER BY execution_run_id DESC LIMIT 1",
            (card_id, task_id),
        ).fetchone()
        if candidate_row is None:
            raise ValueError("no structurally admitted candidate found")
        if candidate_row["task_card_id"] != card_id:
            raise ValueError("candidate task_card_id mismatch")
        if candidate_row["task_id"] != task_id:
            raise ValueError("candidate task_id mismatch")
        if candidate_row["reviewer"] != fixed_reviewer:
            raise ValueError("candidate reviewer mismatch")
        if candidate_row["submitted_by"] != execution_profile:
            raise ValueError("candidate submitted_by does not match execution profile")

        candidate_run = context.connection.execute(
            "SELECT status, outcome, ended_at FROM task_runs "
            "WHERE id = ? AND task_id = ?",
            (candidate_row["execution_run_id"], task_id),
        ).fetchone()
        if candidate_run is None:
            raise ValueError("candidate execution run not found")
        if candidate_run["outcome"] != "review_requested":
            raise ValueError("candidate execution run outcome must be review_requested")
        if not (
            type(candidate_run["ended_at"]) is int and candidate_run["ended_at"] > 0
        ):
            raise ValueError("candidate execution run must have a positive ended_at")

        verdict_row = context.connection.execute(
            "SELECT 1 FROM task_reviewer_verdicts WHERE candidate_id = ?",
            (candidate_row["candidate_id"],),
        ).fetchone()
        if verdict_row is not None:
            raise ValueError("candidate already has a reviewer verdict")

    result = context.mutation_executor._request_changes_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        reason=reason,
        expected_run_id=current_run_id,
    )
    if (
        type(result) is not tuple
        or len(result) != 2
        or result[0] is not True
        or not (type(result[1]) is str and result[1].strip())
    ):
        raise ValueError("native request_changes did not succeed")

    returned_implementer = result[1]
    if handoff is not None and returned_implementer != handoff.execution_profile:
        raise ValueError("native implementer does not match execution profile")

    _advance_task_version(context, task_id, board)
    return {"task_id": task_id, "execution_profile": returned_implementer}


def _handle_request_review(context: Any) -> dict[str, Any]:
    payload = context.payload
    allowed_fields = {"task_id", "summary", "reviewer", "metadata", "board"}
    unknown = set(payload.keys()) - allowed_fields
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    task_id = payload.get("task_id")
    summary = payload.get("summary")
    reviewer = payload.get("reviewer")
    metadata = payload.get("metadata")
    board = payload.get("board")

    for field_name, value in (
        ("task_id", task_id),
        ("summary", summary),
        ("board", board),
    ):
        if not (type(value) is str and value.strip()):
            raise ValueError(f"{field_name} must be a nonblank string")
    if reviewer is not None and not (type(reviewer) is str and reviewer.strip()):
        raise ValueError("reviewer must be absent or a nonblank string")
    if metadata is not None and type(metadata) is not dict:
        raise ValueError("metadata must be absent or a dict")

    task_id = task_id.strip()
    summary = summary.strip()
    board = board.strip()
    reviewer = reviewer.strip() if reviewer is not None else None

    card, native = _load_versioned_task(context, task_id, board)
    card_id = card["id"]
    if not (type(card_id) is int and card_id > 0):
        raise ValueError("card id must be a positive integer")

    handoff = _load_validated_handoff_requirement(context, card_id, task_id)
    if handoff is None:
        expected_run_id = native["current_run_id"]
        if expected_run_id is not None and not (
            type(expected_run_id) is int and expected_run_id > 0
        ):
            raise ValueError("native current_run_id must be a positive integer")

        result = context.mutation_executor._request_review_in_active_transaction(
            context.capability,
            context.binding,
            task_id=task_id,
            summary=summary,
            metadata=metadata,
            reviewer=reviewer,
            expected_run_id=expected_run_id,
            force=False,
            with_reason=True,
        )
        if (
            type(result) is not tuple
            or len(result) != 2
            or result[0] is not True
            or result[1] is not None
        ):
            raise ValueError("native request_review did not succeed")

        _advance_task_version(context, task_id, board)
        if reviewer is None:
            new_native = context.connection.execute(
                "SELECT assignee FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if new_native is None:
                raise ValueError("task not found after mutation")
            reviewer = new_native["assignee"]
        return {"task_id": task_id, "candidate_id": None, "reviewer": reviewer}

    execution_profile = handoff.execution_profile
    fixed_reviewer = handoff.reviewer
    normalized = handoff.normalized

    if native["status"] != "running":
        raise ValueError("task must be in running state for governed review")
    if not (type(native["current_run_id"]) is int and native["current_run_id"] > 0):
        raise ValueError("task must have a positive current_run_id")
    if not (type(native["claim_lock"]) is str and native["claim_lock"].strip()):
        raise ValueError("task must have a nonblank claim_lock")
    if native["assignee"] != execution_profile:
        raise ValueError("task assignee does not match execution profile")

    run_id = native["current_run_id"]
    run_row = context.connection.execute(
        "SELECT status, ended_at, profile FROM task_runs WHERE id = ? AND task_id = ?",
        (run_id, task_id),
    ).fetchone()
    if run_row is None:
        raise ValueError("execution run not found")
    if run_row["status"] != "running":
        raise ValueError("execution run must be in running state")
    if run_row["ended_at"] is not None:
        raise ValueError("execution run must not be ended")
    if run_row["profile"] != execution_profile:
        raise ValueError("execution run profile does not match execution profile")

    findings = []
    if context.binding.actor_profile != execution_profile:
        findings.append(
            HandoffFinding("actor_profile", "does not match execution profile")
        )
    if reviewer is not None and reviewer != fixed_reviewer:
        findings.append(HandoffFinding("reviewer", "does not match handoff reviewer"))
    try:
        validate_candidate_metadata(normalized, metadata)
    except HandoffValidationRejected as exc:
        findings.extend(exc.findings)

    lifecycle_record = LifecycleContractRepository(context.connection).load(task_id)
    if lifecycle_record is not None:
        try:
            validate_lifecycle_output(
                lifecycle_record.snapshot.output_validator,
                metadata,
            )
        except OutputValidationRejected as exc:
            findings.extend(
                HandoffFinding(item.field, item.reason) for item in exc.findings
            )

    if findings:
        findings_json = json.dumps(
            [
                {"field": finding.field, "reason": finding.reason}
                for finding in findings
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        context.connection.execute(
            "INSERT INTO task_handoff_rejections "
            "(rejection_id, task_card_id, task_id, execution_run_id, "
            "attempt_id, submitted_by, findings_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                card_id,
                task_id,
                run_id,
                context.attempt_id,
                context.binding.actor_profile,
                findings_json,
                int(time.time()),
            ),
        )
        failed_checks = tuple(
            FailedCheck(
                code=f"HANDOFF_FIELD_INVALID_{index + 1:03d}",
                target=finding.field,
                expected="the immutable handoff requirement",
                observed="missing or invalid",
                accepted_format=finding.reason,
                remediation="correct this field and retry the same review request",
                responsible_actor="session_agent",
                retry="same_operation",
            )
            for index, finding in enumerate(findings)
        )
        raise _AuditedMutationRejection(failed_checks)

    result = context.mutation_executor._request_review_in_active_transaction(
        context.capability,
        context.binding,
        task_id=task_id,
        summary=summary,
        metadata=metadata,
        reviewer=fixed_reviewer,
        expected_run_id=run_id,
        force=False,
        with_reason=True,
    )
    if (
        type(result) is not tuple
        or len(result) != 2
        or result[0] is not True
        or result[1] is not None
    ):
        raise ValueError("native request_review did not succeed")

    ended_run = context.connection.execute(
        "SELECT outcome, ended_at, summary, metadata FROM task_runs "
        "WHERE id = ? AND task_id = ?",
        (run_id, task_id),
    ).fetchone()
    if ended_run is None:
        raise ValueError("execution run not found after mutation")
    if ended_run["outcome"] != "review_requested":
        raise ValueError("execution run outcome must be review_requested")
    if not (type(ended_run["ended_at"]) is int and ended_run["ended_at"] > 0):
        raise ValueError("execution run must have a positive ended_at")
    if ended_run["summary"] is not None and type(ended_run["summary"]) is not str:
        raise ValueError("execution run summary must be a string or null")
    if ended_run["metadata"] is None:
        raise ValueError("execution run metadata must not be null")
    try:
        persisted_metadata = json.loads(ended_run["metadata"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("execution run metadata is not valid JSON") from exc
    if type(persisted_metadata) is not dict:
        raise ValueError("execution run metadata must be a dict")

    candidate_id = str(uuid.uuid4())
    context.connection.execute(
        "INSERT INTO task_candidate_handoffs "
        "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
        "summary, metadata_json, submitted_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate_id,
            card_id,
            task_id,
            run_id,
            fixed_reviewer,
            ended_run["summary"],
            json.dumps(
                persisted_metadata,
                sort_keys=True,
                separators=(",", ":"),
            ),
            context.binding.actor_profile,
            int(time.time()),
        ),
    )
    _advance_task_version(context, task_id, board)
    return {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "reviewer": fixed_reviewer,
    }


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


class _AuditedMutationRejection(Exception):
    def __init__(self, failed_checks: tuple[FailedCheck, ...]) -> None:
        if type(failed_checks) is not tuple:
            raise TypeError("failed_checks must be a tuple")
        if not failed_checks:
            raise ValueError("failed_checks must be nonempty")
        if any(type(check) is not FailedCheck for check in failed_checks):
            raise TypeError("failed_checks must contain only FailedCheck instances")
        self.failed_checks = failed_checks


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


def _rejection_from_checks(
    attempt_id: str,
    operation: str,
    checks: tuple[FailedCheck, ...],
    not_evaluated_checks: tuple[NotEvaluatedCheck, ...] = (),
) -> dict[str, Any]:
    collector = DiagnosticCollector(
        attempt_id=attempt_id,
        operation=operation,
        boundary=Boundary(
            source=_BOUNDARY_SOURCE,
            destination=_BOUNDARY_DESTINATION,
        ),
    )
    for check in checks:
        collector.failure(check)
    for check in not_evaluated_checks:
        collector.not_evaluated(check)
    return collector.rejection().as_dict()


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
            expected=("a recognized operation admitted by the shared command boundary"),
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
    actor_profile: str | None = None,
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
    if actor_profile is not None:
        identity["actor_profile"] = actor_profile
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
        known_profiles: frozenset[str] = frozenset(),
        prepared_attachment: PreparedAttachment | None = None,
        idempotency_key: str | None = None,
        prepared_manifest: PreparedManifest | None = None,
        prepared_segment_manifest: PreparedSegmentManifest | None = None,
        prepared_successor_manifest: PreparedManifest | None = None,
    ) -> None:
        self.operation = operation
        self.payload = payload
        self.connection = connection
        self.attempt_id = attempt_id
        self.capability = capability
        self.binding = binding
        self.mutation_executor = mutation_executor
        self.known_profiles = known_profiles
        self.prepared_attachment = prepared_attachment
        self.idempotency_key = idempotency_key
        self.prepared_manifest = prepared_manifest
        self.prepared_segment_manifest = prepared_segment_manifest
        self.prepared_successor_manifest = prepared_successor_manifest


class _CommandBoundary:
    def __init__(
        self,
        *,
        database_path: str,
        provider: AdrianKanbanAuthorityProvider,
        handlers: dict[str, Any],
        state_resolver: Any = None,
        known_profiles: Any = (),
        task_input_preparer: Any = None,
        segment_manifest_preparer: Any = None,
    ) -> None:
        if type(provider) is not AdrianKanbanAuthorityProvider:
            raise TypeError("provider must be an AdrianKanbanAuthorityProvider")
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("database_path must be a nonblank string")
        if state_resolver is not None and not callable(state_resolver):
            raise TypeError("state_resolver must be callable or None")
        if task_input_preparer is not None and not callable(task_input_preparer):
            raise TypeError("task_input_preparer must be callable or None")
        if segment_manifest_preparer is not None and not callable(
            segment_manifest_preparer
        ):
            raise TypeError("segment_manifest_preparer must be callable or None")
        if type(known_profiles) not in {tuple, frozenset, set}:
            raise TypeError("known_profiles must be a tuple, frozenset, or set")
        normalized_profiles: list[str] = []
        for profile in known_profiles:
            if not (type(profile) is str and profile.strip()):
                raise TypeError("known_profiles must contain nonblank strings")
            normalized_profiles.append(profile.strip())
        if len(normalized_profiles) != len(set(normalized_profiles)):
            raise ValueError("known_profiles must be unique after trimming")
        self._database_path = database_path
        self._provider = provider
        self._handlers = MappingProxyType(dict(handlers))
        self._state_resolver = state_resolver
        self._known_profiles = frozenset(normalized_profiles)
        self._task_input_preparer = task_input_preparer
        self._segment_manifest_preparer = segment_manifest_preparer

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
                known_profiles=self._known_profiles,
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
        except CommandRejected as exc:
            return _rejection_from_checks(attempt_id, action, exc.failed_checks, exc.not_evaluated_checks)
        except Exception:
            return self._rejection_internal(
                attempt_id, action, _COMMAND_EXECUTION_FAILED
            )
        finally:
            conn.close()

    def _execute_mutation(self, action: str, handler: Any, attempt_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        result = self._execute_mutation_transaction(action, handler, attempt_id, fields)

        if (
            result.get('result') == 'ACCEPTED'
            and action == 'kanban_update_initiative'
            and result.get('value', {}).get('update_kind') == 'purge_replace_task'
            and result.get('value', {}).get('cleanup_required') is True
        ):
            try:
                with contextlib.closing(sqlite3.connect(self._database_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys=ON")
                    replacement_id = result['value']['replacement_id']
                    cleanup_result = execute_cleanup(conn, replacement_id)
                    result['post_commit'] = cleanup_result
            except Exception as e:
                err_msg = str(e)[:1024]
                result['post_commit'] = {'state': 'failed', 'items': [], 'error': str(e)[:1024], 'remediation': 'Replay the original approved request.'}

        return result

    def _execute_mutation_transaction(
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
        actor_profile = fields.get("actor_profile")

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
        if actor_profile is not None and not (
            type(actor_profile) is str and actor_profile.strip()
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
                None if derive_expected_version else fields.get("expected_version")
            ),
            actor_profile=(
                actor_profile.strip() if actor_profile is not None else None
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

            prepared_manifest = None
            if "task_input_manifest_v1" in payload:
                if self._task_input_preparer is None:
                    raise ValueError("task_input_preparer is required")
                preparation_context = TaskInputPreparationContext(
                    session_id=session_id.strip(),
                    execution_context=execution_context.strip(),
                    workspace_id=(
                        workspace_id.strip() if workspace_id is not None else None
                    ),
                    actor_profile=(
                        actor_profile.strip() if actor_profile is not None else None
                    ),
                )
                prepared_manifest = self._task_input_preparer(
                    payload, preparation_context
                )
                if type(prepared_manifest) is not PreparedManifest:
                    raise ValueError("task_input_preparer must return PreparedManifest")

            prepared_successor_manifest = None
            if (
                action == "kanban_update_initiative"
                and payload.get("update_kind") == "purge_replace_task"
            ):
                update = payload.get("update")
                if not isinstance(update, dict):
                    raise ValueError("update must be an object")
                successor_payload = update.get("successor_payload")
                if not isinstance(successor_payload, dict):
                    raise ValueError("successor_payload must be an object")
                if "task_input_manifest_v1" in successor_payload:
                    if self._task_input_preparer is None:
                        raise ValueError("task_input_preparer is required")
                    preparation_context = TaskInputPreparationContext(
                        session_id=session_id.strip(),
                        execution_context=execution_context.strip(),
                        workspace_id=(
                            workspace_id.strip() if workspace_id is not None else None
                        ),
                        actor_profile=(
                            actor_profile.strip() if actor_profile is not None else None
                        ),
                    )
                    prepared_successor_manifest = self._task_input_preparer(
                        successor_payload, preparation_context
                    )
                    if type(prepared_successor_manifest) is not PreparedManifest:
                        raise ValueError(
                            "task_input_preparer must return PreparedManifest"
                        )

            prepared_segment_manifest = None
            if (
                action == "kanban_update_initiative"
                and payload.get("update_kind") == "segment_manifest_projection"
            ):
                if self._segment_manifest_preparer is None:
                    raise ValueError("segment_manifest_preparer is required")
                preparation_context = TaskInputPreparationContext(
                    session_id=session_id.strip(),
                    execution_context=execution_context.strip(),
                    workspace_id=(
                        workspace_id.strip() if workspace_id is not None else None
                    ),
                    actor_profile=(
                        actor_profile.strip() if actor_profile is not None else None
                    ),
                )
                prepared_segment_manifest = self._segment_manifest_preparer(
                    payload, preparation_context
                )
                if type(prepared_segment_manifest) is not PreparedSegmentManifest:
                    raise ValueError(
                        "segment_manifest_preparer must return "
                        "PreparedSegmentManifest"
                    )

            prepared_attachment = None
            if action == "kanban_attach_url":
                prepared_attachment = _prepare_attach_url_payload(payload)

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

            if action in INITIATIVE_OPERATIONS:
                mutation_payload = {
                    key: value
                    for key, value in payload.items()
                    if key != "approval_id"
                }
            else:
                mutation_payload = payload

            binding = CapabilityBinding(
                operation=action,
                target=target,
                expected_version=expected_version,
                canonical_digest=_canonical_digest(mutation_payload),
                session_id=session_id,
                workspace_id=workspace_id,
                plugin_version=PLUGIN_VERSION,
                protocol_version=PROTOCOL_VERSION,
                execution_context=execution_context,
                actor_profile=(
                    actor_profile.strip() if actor_profile is not None else None
                ),
            )
            capability = self._provider._mint_after_admission(binding)
            adapter = self._provider._create_mutation_executor(conn)

            audited_rejection: _AuditedMutationRejection | None = None
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
                    known_profiles=self._known_profiles,
                    prepared_attachment=prepared_attachment,
                    idempotency_key=idempotency_key,
                    prepared_manifest=prepared_manifest,
                    prepared_segment_manifest=prepared_segment_manifest,
                    prepared_successor_manifest=prepared_successor_manifest,
                )
                try:
                    result = handler(context)
                except _AuditedMutationRejection as exc:
                    audited_rejection = exc
                else:
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
            if audited_rejection is not None:
                return _rejection_from_checks(
                    attempt_id, action, audited_rejection.failed_checks
                )
        except CommandRejected as exc:
            return _rejection_from_checks(attempt_id, action, exc.failed_checks, exc.not_evaluated_checks)
        except _ConflictError:
            return self._rejection_internal(attempt_id, action, _IDEMPOTENCY_CONFLICT)
        except _StaleDerivedStateError:
            return self._rejection_internal(attempt_id, action, _STALE_DERIVED_STATE)
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
    "_handle_attach",
    "_handle_attach_url",
    "_handle_block",
    "_handle_show",
    "_handle_list",
    "_handle_attachments",
    "_handle_comment",
    "_handle_create",
    "_handle_heartbeat",
    "_handle_link",
    "_handle_unblock",
    "_handle_close_initiative",
    "_handle_create_initiative",
    "_handle_update_initiative",
    "_handle_transition_initiative",
]

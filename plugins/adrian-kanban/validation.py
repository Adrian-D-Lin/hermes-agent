"""adrian-kanban foundational initiative/task validation (S1).

These primitives enforce the unified-card identity rules, transition
monotonicity, and predecessor integrity called for by the ratified design
(§4.1, §4 terms, §7.12, §5 invariant 16) and the S1 brief (§5.3). They are
internal persistence primitives: they do NOT implement S2 lifecycle admission,
transition policy, workspace operations, dispatcher behavior, or Write-Gate
consumption.

Identity rules encoded here:

* A unified **initiative** card requires a non-null initiative identity.
* A unified **task** card requires BOTH an initiative identity and a task
  identity.
* Initiative transitions enforce strictly-increasing transition IDs (a
  transition_id must be greater than the current highest for the initiative)
  and predecessor integrity (a non-first transition must name the current
  highest recorded transition as its predecessor).
"""

from __future__ import annotations

import json
from typing import Optional


class InitiativeIdentityError(ValueError):
    """Raised when an initiative card is created without an initiative id."""


class TaskIdentityError(ValueError):
    """Raised when a task card is created without an initiative/task id."""


class ReleaseTransitionError(ValueError):
    """Raised when a release status transition is invalid."""


class TransitionIntegrityError(ValueError):
    """Raised when an initiative transition breaks monotonicity/predecessor."""


class SessionStartupError(ValueError):
    """Base error for session startup persistence validation."""


class SessionStartupStateError(SessionStartupError):
    """Raised when a session startup state or transition is invalid."""


class SessionStartupRevisionError(SessionStartupError):
    """Raised when a session startup revision mismatch is detected."""


class SessionStartupRecordMissingError(SessionStartupError):
    """Raised when a session startup record does not exist."""


SESSION_STARTUP_STATES = (
    "awaiting_project_selection",
    "awaiting_initiative_selection",
    "resolving_lifecycle_and_work",
    "verifying_workspace",
    "awaiting_writegate_confirmation",
    "anchored",
    "failed_recoverable",
    "cancelled",
)

SESSION_STARTUP_ACTIVE_STATES = SESSION_STARTUP_STATES[:5]

RELEASE_STATUSES = ("pending", "issued", "observed")

_RELEASE_TRANSITIONS = {
    "pending": {"issued"},
    "issued": {"observed"},
    "observed": set(),
}

_SESSION_STARTUP_ALLOWED_TRANSITIONS = {
    "awaiting_project_selection": {
        "awaiting_initiative_selection",
        "failed_recoverable",
        "cancelled",
    },
    "awaiting_initiative_selection": {
        "resolving_lifecycle_and_work",
        "failed_recoverable",
        "cancelled",
    },
    "resolving_lifecycle_and_work": {
        "verifying_workspace",
        "failed_recoverable",
        "cancelled",
    },
    "verifying_workspace": {
        "awaiting_writegate_confirmation",
        "failed_recoverable",
        "cancelled",
    },
    "awaiting_writegate_confirmation": {
        "anchored",
        "failed_recoverable",
        "cancelled",
    },
    "failed_recoverable": set(SESSION_STARTUP_ACTIVE_STATES) | {"cancelled"},
    "anchored": set(),
    "cancelled": set(),
}


def validate_session_id(session_id: object) -> str:
    """Return *session_id* or raise :class:`SessionStartupError`."""
    if session_id is None:
        raise SessionStartupError("session_id is required")
    value = str(session_id).strip()
    if not value:
        raise SessionStartupError("session_id must be non-empty")
    return value


def validate_held_opening_prompt(prompt: object) -> str:
    """Return *prompt* or raise :class:`SessionStartupError`."""
    if prompt is None:
        raise SessionStartupError("opening_prompt is required")
    value = str(prompt)
    if not value.strip():
        raise SessionStartupError("opening_prompt must be non-empty")
    return value


def validate_protocol_version(protocol_version: object) -> str:
    """Return *protocol_version* or raise :class:`SessionStartupError`."""
    if protocol_version is None:
        raise SessionStartupError("protocol_version is required")
    value = str(protocol_version).strip()
    if not value:
        raise SessionStartupError("protocol_version must be non-empty")
    return value


def validate_session_startup_state(state: object) -> str:
    """Return *state* if valid, else raise :class:`SessionStartupStateError`."""
    value = str(state).strip() if state is not None else ""
    if value not in SESSION_STARTUP_STATES:
        raise SessionStartupStateError(f"invalid session startup state {state!r}")
    return value


def validate_session_startup_transition(
    current_state: object, target_state: object
) -> tuple[str, str]:
    """Validate and return a session startup state transition."""
    current = validate_session_startup_state(current_state)
    target = validate_session_startup_state(target_state)
    if target not in _SESSION_STARTUP_ALLOWED_TRANSITIONS[current]:
        raise SessionStartupStateError(
            f"session startup transition from {current!r} to {target!r} "
            "is not permitted"
        )
    return current, target


def validate_session_startup_creation_draft(
    creation_stage: object, creation_draft: object
) -> tuple[Optional[str], Optional[str]]:
    """Validate a creation substate and return its canonical JSON form."""
    if creation_stage is None:
        if creation_draft is not None:
            raise SessionStartupStateError(
                "creation_draft must be None when creation_stage is None"
            )
        return None, None

    stage = str(creation_stage).strip()
    if stage not in ("awaiting_title", "awaiting_objective", "awaiting_approval"):
        raise SessionStartupStateError(f"invalid creation_stage {creation_stage!r}")
    if not isinstance(creation_draft, dict):
        raise SessionStartupStateError("creation_draft must be a dict")

    if stage == "awaiting_title":
        if creation_draft != {}:
            raise SessionStartupStateError(
                "awaiting_title requires an empty creation draft"
            )
    elif stage == "awaiting_objective":
        if set(creation_draft) != {"title"}:
            raise SessionStartupStateError(
                "awaiting_objective requires exactly the title field"
            )
        title = creation_draft["title"]
        if not isinstance(title, str) or not title.strip():
            raise SessionStartupStateError("title must be a nonblank string")
    else:
        required = {
            "title",
            "objective",
            "initiative_id",
            "body",
            "request_id",
            "approval_id",
        }
        if set(creation_draft) != required:
            raise SessionStartupStateError(
                "awaiting_approval requires exactly title, objective, "
                "initiative_id, body, request_id, and approval_id"
            )
        for field in required:
            value = creation_draft[field]
            if not isinstance(value, str) or not value.strip():
                raise SessionStartupStateError(
                    f"creation draft field {field!r} must be a nonblank string"
                )

    return stage, json.dumps(
        creation_draft,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def validate_release_status(status: object) -> str:
    """Return *status* if valid, else raise :class:`ReleaseTransitionError`."""
    value = str(status).strip() if status is not None else ""
    if value not in RELEASE_STATUSES:
        raise ReleaseTransitionError(f"invalid release status {status!r}")
    return value


def validate_release_transition(current: object, target: object) -> tuple[str, str]:
    """Validate and return a release status transition."""
    cur = validate_release_status(current)
    tgt = validate_release_status(target)
    if tgt != cur and tgt not in _RELEASE_TRANSITIONS[cur]:
        raise ReleaseTransitionError(
            f"release transition from {cur!r} to {tgt!r} is not permitted"
        )
    return cur, tgt


def _require_meaningful_text(value: object, field: str) -> str:
    if value is None:
        raise SessionStartupStateError(
            f"session startup field {field!r} is required"
        )
    text = str(value).strip()
    if not text:
        raise SessionStartupStateError(
            f"session startup field {field!r} must be non-empty"
        )
    return text


def validate_session_startup_fields(
    *,
    to_state: str,
    selected_project_id: object = None,
    selected_initiative_id: object = None,
    accepted_phase: object = None,
    accepted_segment_id: object = None,
    logical_workspace_id: object = None,
    writegate_binding_version: object = None,
    writegate_binding_ref: object = None,
    failure_detail: object = None,
) -> None:
    """Validate required fields for a session startup target state."""
    if accepted_segment_id is not None:
        _require_meaningful_text(accepted_segment_id, "accepted_segment_id")
    if to_state in {
        "awaiting_initiative_selection",
        "resolving_lifecycle_and_work",
        "verifying_workspace",
        "awaiting_writegate_confirmation",
        "anchored",
    }:
        _require_meaningful_text(selected_project_id, "selected_project_id")
    if to_state in {
        "resolving_lifecycle_and_work",
        "verifying_workspace",
        "awaiting_writegate_confirmation",
        "anchored",
    }:
        _require_meaningful_text(selected_initiative_id, "selected_initiative_id")
    if to_state in {
        "verifying_workspace",
        "awaiting_writegate_confirmation",
        "anchored",
    }:
        _require_meaningful_text(accepted_phase, "accepted_phase")
        _require_meaningful_text(logical_workspace_id, "logical_workspace_id")
    if to_state in {"awaiting_writegate_confirmation", "anchored"}:
        _require_meaningful_text(
            writegate_binding_version, "writegate_binding_version"
        )
        _require_meaningful_text(writegate_binding_ref, "writegate_binding_ref")
    if to_state == "failed_recoverable":
        _require_meaningful_text(failure_detail, "failure_detail")


def validate_initiative_identity(initiative_id: object) -> str:
    """Return *initiative_id* or raise :class:`InitiativeIdentityError`."""
    if initiative_id is None:
        raise InitiativeIdentityError(
            "initiative card requires a non-null initiative identity"
        )
    value = str(initiative_id).strip()
    if not value:
        raise InitiativeIdentityError(
            "initiative card requires a non-empty initiative identity"
        )
    return value


def validate_task_identity(
    initiative_id: object, task_id: object
) -> tuple[str, str]:
    """Return ``(initiative_id, task_id)`` or raise on a missing identity.

    A task requires BOTH an initiative identity and a task identity.
    """
    init_id = validate_initiative_identity(initiative_id)
    if task_id is None:
        raise TaskIdentityError(
            "task card requires both an initiative identity and a task identity"
        )
    value = str(task_id).strip()
    if not value:
        raise TaskIdentityError(
            "task card requires both an initiative identity and a task identity"
        )
    return init_id, value


def validate_transition_id(current: Optional[int], next_id: Optional[int]) -> int:
    """Return *next_id* if it is strictly greater than *current*.

    Transition IDs must strictly increase. ``current`` may be ``None`` for the
    very first transition (any id is accepted). Equality is rejected: a
    duplicate or lower transition_id is not permitted. Raises
    :class:`TransitionIntegrityError` on a non-increasing step.
    """
    if next_id is None:
        raise TransitionIntegrityError("transition requires a transition_id")
    if current is not None and next_id <= current:
        raise TransitionIntegrityError(
            f"transition_id {next_id} is not strictly greater than "
            f"current {current}"
        )
    return next_id

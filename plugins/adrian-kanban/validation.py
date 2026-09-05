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

from typing import Optional


class InitiativeIdentityError(ValueError):
    """Raised when an initiative card is created without an initiative id."""


class TaskIdentityError(ValueError):
    """Raised when a task card is created without an initiative/task id."""


class TransitionIntegrityError(ValueError):
    """Raised when an initiative transition breaks monotonicity/predecessor."""


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

"""Immutable override proposal storage plus Write-Gate preparation.

This module is internal persistence for the S3 gate-override integration layer.
It stores one immutable, already-derived proposal snapshot and prepares a
single Write-Gate approval record on the caller's open transaction. It does
not begin, commit, or roll back transactions; it does not create schema at
runtime; it does not implement gate assessment, callbacks, execution, or any
public model-tool path. The subsequent dedicated handlers derive facts from
actual state and call this helper; arbitrary caller-supplied proposal facts do
not constitute a verified override.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from writegate.kanban_approvals import (
    KanbanApprovalRejected,
    KanbanInitiativeApprovalHost,
    KanbanInitiativeApprovalPreparation,
)

from .actor import ActorEvidenceRejected, validate_authorizer_evidence

__all__ = ["store_prepared_override"]

_ALLOWED_OPERATIONS = {
    "kanban_execute_initiative_gate_override": "initiative",
    "kanban_execute_task_gate_override": "task",
}

_PROPOSAL_KEYS = frozenset(
    {
        "operation",
        "initiative_id",
        "target",
        "target_kind",
        "board",
        "expected_version",
        "source",
        "destination",
        "gates",
        "non_bypassable_checks",
        "claim_run_closures",
        "dependency_changes",
        "downstream_exceptions",
        "reconciliation_ref",
        "override_reason",
        "commentary",
    }
)


def _require_nonblank_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _validate_closure_fields(source: object, destination: object, actual_closed_at) -> None:
    """Validate explicit source/destination ``closed_at`` fields against state.

    A currently closed initiative requires an explicit source timestamp equal to
    its actual ``closed_at`` and an explicit destination of ``None`` (the reopen).
    An open initiative may omit legacy closure fields (meaning ``None``), but any
    supplied field must match its open state. An override never closes, so a
    non-null destination is always rejected. Bool/string values are rejected even
    when they look like timestamps.
    """
    if not isinstance(source, dict) or not isinstance(destination, dict):
        raise ValueError("source and destination must be mappings")
    source_closed = source.get("closed_at")
    destination_closed = destination.get("closed_at")
    for value in (source_closed, destination_closed):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("closure fields must be null or an integer timestamp")
    if destination_closed is not None:
        # An override cannot close an initiative.
        raise ValueError("an override cannot close an initiative")
    if actual_closed_at is None:
        # Open: legacy omission means None; any supplied source field must match.
        if source_closed is not None:
            raise ValueError("source closure does not match the open initiative")
    else:
        # Closed: require exact source timestamp and explicit destination null.
        if "closed_at" not in source or source_closed != actual_closed_at:
            raise ValueError("source closure does not match the current initiative")
        if "closed_at" not in destination:
            raise ValueError("a closed initiative requires an explicit destination closure")


def _in_transaction(conn) -> None:
    try:
        in_tx = conn.in_transaction
    except AttributeError:
        raise ValueError("an existing caller transaction is required")
    if not in_tx:
        raise ValueError("an existing caller transaction is required")


def store_prepared_override(
    conn,
    *,
    request_id,
    approval_id,
    initial_authorizer,
    initial_session_id,
    initial_message_id,
    initial_quote,
    executor_session_id,
    executor_profile,
    proposal,
    now,
    expires_at,
):
    """Store one immutable override proposal and prepare its Write-Gate record.

    Requires an open caller transaction. Never begins, commits, or rolls back.
    All validation failures surface as ValueError so the caller's rollback
    removes any partial metadata/approval inserts.
    """
    _in_transaction(conn)

    # Exact identity strings.
    _require_nonblank_str(request_id, "request_id")
    _require_nonblank_str(approval_id, "approval_id")
    _require_nonblank_str(initial_session_id, "initial_session_id")
    _require_nonblank_str(initial_message_id, "initial_message_id")
    _require_nonblank_str(executor_session_id, "executor_session_id")
    _require_nonblank_str(executor_profile, "executor_profile")

    # Exact quote bytes preserved for UTF-8 SHA256.
    if not isinstance(initial_quote, str) or not initial_quote.strip():
        raise ValueError("initial_quote must be a nonblank string")

    # Positive strict integer clocks; expiry strictly after now.
    _require_positive_int(now, "now")
    _require_positive_int(expires_at, "expires_at")
    if expires_at <= now:
        raise ValueError("expires_at must be greater than now")

    # Validate the actual registered initial authorizer against the host-owned
    # durable human interaction named by the convention. Future ingress must
    # actually bind that message; tool arguments are never trusted here.
    try:
        validated = validate_authorizer_evidence(
            initial_authorizer, expected_request_id=initial_message_id, now=now
        )
    except ActorEvidenceRejected as exc:
        raise ValueError(str(exc)) from None

    # Proposal is the complete already-derived snapshot: preserve all fields,
    # never manufacture satisfied gates. The derived snapshot must contain
    # exactly the fixed set of proposal fields; no more, no fewer.
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be a mapping")
    if set(proposal.keys()) != _PROPOSAL_KEYS:
        raise ValueError("proposal must contain exactly the derived snapshot keys")

    operation = proposal["operation"]
    target_kind = proposal["target_kind"]
    if operation not in _ALLOWED_OPERATIONS:
        raise ValueError("unsupported override operation")
    if _ALLOWED_OPERATIONS[operation] != target_kind:
        raise ValueError("operation/target_kind pair is not accepted")

    initiative_id = proposal["initiative_id"]
    board = proposal["board"]
    target = proposal["target"]
    expected_version = proposal["expected_version"]
    _require_nonblank_str(initiative_id, "initiative_id")
    _require_nonblank_str(board, "board")
    _require_nonblank_str(target, "target")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError("expected_version must be a non-negative integer")

    # Non-bypassable checks must all be met; a failed check yields no approvable
    # record. No gate assessment or callbacks are performed here.
    checks = proposal["non_bypassable_checks"]
    if not isinstance(checks, list):
        raise ValueError("non_bypassable_checks must be a list")
    for check in checks:
        if not isinstance(check, dict) or check.get("result") != "met":
            raise ValueError("a non-bypassable check is not met")

    if target_kind == "initiative":
        if target != initiative_id:
            raise ValueError("initiative target must equal initiative_id")
        card = conn.execute(
            "SELECT id, initiative_id, task_id, board_slug, record_version, closed_at "
            "FROM adrian_kanban_cards WHERE initiative_id = ? AND task_id IS NULL",
            (initiative_id,),
        ).fetchone()
        if card is None or card["board_slug"] != board:
            raise ValueError("initiative not found in the declared board")
        if card["record_version"] != expected_version:
            raise ValueError("expected version does not match the current initiative")
        _validate_closure_fields(
            proposal["source"], proposal["destination"], card["closed_at"]
        )
    else:
        card = conn.execute(
            "SELECT c.id, c.initiative_id, c.task_id, c.board_slug, c.record_version, c.closed_at "
            "FROM adrian_kanban_cards c WHERE c.task_id = ?",
            (target,),
        ).fetchone()
        if (
            card is None
            or card["initiative_id"] != initiative_id
            or card["board_slug"] != board
            or card["closed_at"] is not None
        ):
            raise ValueError("open task not found under the parent initiative/board")
        if card["record_version"] != expected_version:
            raise ValueError("expected version does not match the current task")
        # Task proposals never carry closure state; reject any non-null field.
        _validate_closure_fields(
            proposal["source"], proposal["destination"], None
        )
        # A task override cannot silently reopen its distinct parent initiative;
        # that needs initiative-specific authority. The parent must exist, be in
        # the same board, and still be open.
        parent = conn.execute(
            "SELECT board_slug, closed_at FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND task_id IS NULL",
            (initiative_id,),
        ).fetchone()
        if parent is None or parent["board_slug"] != board or parent["closed_at"] is not None:
            raise ValueError("open parent initiative not found in the declared board")

    # Reject a reused initial event even with different request/approval/
    # idempotency keys. The existing _CommandBoundary handles exact request
    # replay; this helper does not reinterpret a duplicate event as permission
    # for another proposal.
    existing_event = conn.execute(
        "SELECT 1 FROM gate_override_proposals WHERE initial_message_id = ?",
        (initial_message_id,),
    ).fetchone()
    if existing_event is not None:
        raise ValueError("initial event already bound to a proposal")

    # Deterministic canonical payload; exact UTF-8 SHA256 of the stored string.
    canonical_payload = json.dumps(
        {
            "proposal": proposal,
            "initial_instruction": {
                "quote": initial_quote,
                "quote_sha256": hashlib.sha256(initial_quote.encode("utf-8")).hexdigest(),
                "session_id": initial_session_id,
                "message_id": initial_message_id,
                "authorizer": json.loads(validated.canonical_payload),
            },
            "executor": {
                "session_id": executor_session_id,
                "profile": executor_profile,
            },
            "request_id": request_id,
            "expires_at": expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    try:
        conn.execute(
            "INSERT INTO gate_override_proposals "
            "(request_id, approval_id, initial_event_id, initial_session_id, "
            " initial_message_id, canonical_payload, canonical_digest, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                approval_id,
                initial_message_id,
                initial_session_id,
                initial_message_id,
                canonical_payload,
                canonical_digest,
                now,
                expires_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("proposal identity collides with an existing proposal") from None

    # Prepare the Write-Gate record on the same connection. The Write-Gate owns
    # prepared/approved/cancelled/consumed states; no plugin approval-state
    # column or independent registry exists.
    try:
        host = KanbanInitiativeApprovalHost(initial_authorizer)
        host.prepare(
            conn,
            KanbanInitiativeApprovalPreparation(
                approval_id=approval_id,
                request_id=request_id,
                operation=operation,
                initiative_id=initiative_id,
                proposed_creation_id=None,
                expected_version=expected_version,
                canonical_digest=canonical_digest,
                session_id=executor_session_id,
                expires_at=expires_at,
                requires_distinct_authorizer=True,
            ),
            now=now,
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("Write-Gate approval identity collides with an existing approval") from None
    except KanbanApprovalRejected as exc:
        raise ValueError(str(exc)) from None

    return {
        "request_id": request_id,
        "approval_id": approval_id,
        "canonical_payload": canonical_payload,
        "canonical_digest": canonical_digest,
    }

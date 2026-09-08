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
    CANONICALIZATION_VERSION,
    KanbanApprovalRejected,
    KanbanInitiativeApprovalConsumption,
    KanbanInitiativeApprovalHost,
    KanbanInitiativeApprovalPreparation,
    consume_approved,
)

from .actor import ActorEvidenceRejected, validate_authorizer_evidence

__all__ = ["store_prepared_override", "consume_revalidated_override"]

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


def consume_revalidated_override(
    conn,
    *,
    request_id,
    operation,
    target,
    executor_session_id,
    executor_profile,
    current_proposal,
    mutation_id,
    idempotency_key,
    now,
):
    """Consume an approved override only when it still matches live state.

    Requires an open caller transaction and never begins, commits, or rolls
    back. The caller supplies the freshly derived execution binding; this helper
    recomputes the exact canonical digest from the supplied ``current_proposal``
    and the stored initial instruction/executor, then requires the stored
    proposal snapshot to still carry that digest before consuming the approval
    through the Write-Gate's own consumption path. Any mismatch means the
    derivation changed since approval and a new approval is required. All
    validation failures surface as ValueError so the caller's rollback removes
    any partial work.
    """
    _in_transaction(conn)

    _require_nonblank_str(request_id, "request_id")
    _require_nonblank_str(operation, "operation")
    _require_nonblank_str(target, "target")
    _require_nonblank_str(executor_session_id, "executor_session_id")
    _require_nonblank_str(executor_profile, "executor_profile")
    _require_nonblank_str(mutation_id, "mutation_id")
    _require_nonblank_str(idempotency_key, "idempotency_key")
    _require_positive_int(now, "now")

    if not isinstance(current_proposal, dict):
        raise ValueError("current_proposal must be a mapping")
    if set(current_proposal.keys()) != _PROPOSAL_KEYS:
        raise ValueError("current_proposal must contain exactly the derived snapshot keys")
    if operation != current_proposal["operation"]:
        raise ValueError("operation does not match the current proposal")
    if target != current_proposal["target"]:
        raise ValueError("target does not match the current proposal")

    # Load the stored proposal snapshot plus the Write-Gate approval record.
    stored = conn.execute(
        "SELECT p.request_id, p.approval_id, p.initial_session_id, "
        "p.initial_message_id, p.canonical_payload, p.canonical_digest, "
        "p.created_at, p.expires_at, "
        "a.state, a.operation, a.initiative_id, a.expected_version, "
        "a.authorizer_evidence, a.session_id, a.approval_evidence "
        "FROM gate_override_proposals p "
        "JOIN write_gate_kanban_approvals a ON a.approval_id = p.approval_id "
        "WHERE p.request_id = ?",
        (request_id,),
    ).fetchone()
    if stored is None:
        raise ValueError("no stored override proposal for this request")

    try:
        stored_payload = json.loads(stored["canonical_payload"])
    except (TypeError, ValueError):
        raise ValueError("the stored canonical payload is not valid JSON") from None
    if not isinstance(stored_payload, dict):
        raise ValueError("the stored canonical payload must be an object")
    if set(stored_payload.keys()) != {
        "proposal",
        "initial_instruction",
        "executor",
        "request_id",
        "expires_at",
    }:
        raise ValueError("the stored canonical payload has unexpected fields")
    if (
        json.dumps(
            stored_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        != stored["canonical_payload"]
    ):
        raise ValueError("the stored canonical payload is not canonical")
    if hashlib.sha256(stored["canonical_payload"].encode("utf-8")).hexdigest() != stored[
        "canonical_digest"
    ]:
        raise ValueError("the stored canonical payload does not match its digest")

    # Validate the complete canonical durable payload shape without leaking
    # KeyError/TypeError/JSON exceptions.
    stored_initial = stored_payload.get("initial_instruction")
    stored_executor = stored_payload.get("executor")
    if not isinstance(stored_initial, dict) or not isinstance(stored_executor, dict):
        raise ValueError("the stored canonical payload is malformed")

    stored_quote = stored_initial.get("quote")
    stored_quote_sha256 = stored_initial.get("quote_sha256")
    stored_session = stored_initial.get("session_id")
    stored_message = stored_initial.get("message_id")
    stored_authorizer = stored_initial.get("authorizer")
    if not isinstance(stored_quote, str) or not stored_quote.strip():
        raise ValueError("the stored canonical payload is malformed")
    if not isinstance(stored_quote_sha256, str) or not stored_quote_sha256.strip():
        raise ValueError("the stored canonical payload is malformed")
    if hashlib.sha256(stored_quote.encode("utf-8")).hexdigest() != stored_quote_sha256:
        raise ValueError("the stored quote does not match its SHA256")
    if not isinstance(stored_session, str) or not stored_session.strip():
        raise ValueError("the stored canonical payload is malformed")
    if not isinstance(stored_message, str) or not stored_message.strip():
        raise ValueError("the stored canonical payload is malformed")
    if not isinstance(stored_authorizer, dict):
        raise ValueError("the stored authorizer evidence is malformed")

    stored_exec_session = stored_executor.get("session_id")
    stored_exec_profile = stored_executor.get("profile")
    if not isinstance(stored_exec_session, str) or not stored_exec_session.strip():
        raise ValueError("the stored canonical payload is malformed")
    if not isinstance(stored_exec_profile, str) or not stored_exec_profile.strip():
        raise ValueError("the stored canonical payload is malformed")

    stored_request_id = stored_payload.get("request_id")
    stored_expires_at = stored_payload.get("expires_at")
    if not isinstance(stored_request_id, str) or not stored_request_id.strip():
        raise ValueError("the stored canonical payload is malformed")
    if isinstance(stored_expires_at, bool) or not isinstance(stored_expires_at, int):
        raise ValueError("the stored canonical payload is malformed")

    # Bind both executor profile and session to the canonical payload.
    if stored_exec_session != executor_session_id:
        raise ValueError("executor session does not match the approved snapshot")
    if stored_exec_profile != executor_profile:
        raise ValueError("executor profile does not match the approved snapshot")
    if stored_request_id != request_id:
        raise ValueError("request id does not match the approved snapshot")

    # Payload/metadata expiry equality and strict liveness.
    if stored_expires_at != stored["expires_at"]:
        raise ValueError("payload expiry does not match the stored metadata")
    if stored_expires_at <= now:
        raise ValueError("the approved override has expired")

    stored_created_at = stored["created_at"]
    if isinstance(stored_created_at, bool) or not isinstance(stored_created_at, int):
        raise ValueError("the stored created_at must be a strict integer")
    if stored_created_at > now:
        raise ValueError("the stored created_at must not be in the future")

    # Load and require the durable distinct-authorizer flag to be exactly 1.
    flag_row = conn.execute(
        "SELECT requires_distinct_authorizer FROM write_gate_kanban_approvals "
        "WHERE approval_id = ?",
        (stored["approval_id"],),
    ).fetchone()
    if flag_row is None or flag_row[0] != 1:
        raise ValueError("the durable approval does not require a distinct authorizer")

    # Exact approved operation, target, parent initiative, expected version,
    # canonicalization version, and authorizer evidence.
    if stored["operation"] != operation:
        raise ValueError("operation does not match the approved record")
    if stored["initiative_id"] != current_proposal["initiative_id"]:
        raise ValueError("parent initiative does not match the approved record")
    if stored["expected_version"] != current_proposal["expected_version"]:
        raise ValueError("expected version does not match the approved record")
    if stored["session_id"] != executor_session_id:
        raise ValueError("executor session does not match the approved record")
    if stored["state"] != "approved":
        raise ValueError("the approval is not in the approved state")

    try:
        stored_authorizer_evidence = json.loads(stored["authorizer_evidence"])
    except (TypeError, ValueError):
        raise ValueError("the stored authorizer evidence is not valid JSON") from None
    if not isinstance(stored_authorizer_evidence, dict):
        raise ValueError("the stored authorizer evidence must be an object")
    if stored_authorizer_evidence != stored_authorizer:
        raise ValueError("authorizer evidence does not match the approved snapshot")

    # Validate the complete canonical durable second-approval receipt. The
    # host produces exactly these fields; no more, no fewer.
    try:
        receipt = json.loads(stored["approval_evidence"])
    except (TypeError, ValueError):
        raise ValueError("the stored approval evidence is not valid JSON") from None
    if not isinstance(receipt, dict):
        raise ValueError("the stored approval evidence must be an object")
    if set(receipt.keys()) != {
        "second_authorizer",
        "request_id",
        "canonical_digest",
        "approval_quote",
        "approval_quote_sha256",
    }:
        raise ValueError("the stored approval evidence has unexpected fields")

    second_authorizer = receipt.get("second_authorizer")
    receipt_request_id = receipt.get("request_id")
    receipt_digest = receipt.get("canonical_digest")
    receipt_quote = receipt.get("approval_quote")
    receipt_quote_sha256 = receipt.get("approval_quote_sha256")

    if not isinstance(second_authorizer, dict):
        raise ValueError("the stored second authorizer evidence is malformed")
    if set(second_authorizer.keys()) != {
        "connection_id",
        "expires_at",
        "issued_at",
        "peer_identity",
        "request_id",
        "route",
        "type",
        "version",
    }:
        raise ValueError("the stored second authorizer evidence has unexpected fields")
    if not isinstance(receipt_request_id, str) or not receipt_request_id.strip():
        raise ValueError("the stored approval request id is malformed")
    if not isinstance(receipt_digest, str) or not receipt_digest.strip():
        raise ValueError("the stored approval digest is malformed")
    if not isinstance(receipt_quote, str) or not receipt_quote.strip():
        raise ValueError("the stored approval quote is malformed")
    if not isinstance(receipt_quote_sha256, str) or not receipt_quote_sha256.strip():
        raise ValueError("the stored approval quote digest is malformed")

    # Request/digest equality against the stored record.
    if receipt_request_id != stored["request_id"]:
        raise ValueError("the approval receipt request id does not match")
    if receipt_digest != stored["canonical_digest"]:
        raise ValueError("the approval receipt digest does not match")
    # Nonblank quote and exact UTF-8 SHA256.
    if hashlib.sha256(receipt_quote.encode("utf-8")).hexdigest() != receipt_quote_sha256:
        raise ValueError("the approval receipt quote does not match its SHA256")

    # A second event distinct from the first event, with strict integer times.
    second_issued_at = second_authorizer.get("issued_at")
    second_expires_at = second_authorizer.get("expires_at")
    second_request_id = second_authorizer.get("request_id")
    if isinstance(second_issued_at, bool) or not isinstance(second_issued_at, int):
        raise ValueError("the second approval event time range is invalid")
    if isinstance(second_expires_at, bool) or not isinstance(second_expires_at, int):
        raise ValueError("the second approval event time range is invalid")
    if not isinstance(second_request_id, str) or not second_request_id.strip():
        raise ValueError("the second approval event request id is missing")
    if second_request_id == stored_message:
        raise ValueError("the second approval event must be distinct from the first")

    # Second evidence current at approved_at; approved_at >= prepared_at. Do not
    # require the second evidence to remain current at execution time.
    approved_at = conn.execute(
        "SELECT approved_at FROM write_gate_kanban_approvals WHERE approval_id = ?",
        (stored["approval_id"],),
    ).fetchone()[0]
    if isinstance(approved_at, bool) or not isinstance(approved_at, int):
        raise ValueError("the stored approved_at must be a strict integer")
    prepared_at = conn.execute(
        "SELECT prepared_at FROM write_gate_kanban_approvals WHERE approval_id = ?",
        (stored["approval_id"],),
    ).fetchone()[0]
    if isinstance(prepared_at, bool) or not isinstance(prepared_at, int):
        raise ValueError("the stored prepared_at must be a strict integer")
    if approved_at < prepared_at:
        raise ValueError("approved_at must not precede prepared_at")
    if not (second_issued_at <= approved_at < second_expires_at):
        raise ValueError("the second approval event must be current at approval time")

    # Recompute the exact canonical digest from the live derived proposal and the
    # stored initial instruction/executor. If it differs from the stored digest
    # the snapshot was tampered with and cannot be consumed.
    canonical_payload = json.dumps(
        {
            "proposal": current_proposal,
            "initial_instruction": stored_initial,
            "executor": stored_executor,
            "request_id": stored_request_id,
            "expires_at": stored_expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    if canonical_digest != stored["canonical_digest"]:
        raise ValueError("the stored proposal snapshot no longer matches its digest")

    # Re-validate the live card state against the derived proposal, mirroring
    # preparation, so a stale version or moved target cannot be executed.
    target_kind = current_proposal["target_kind"]
    initiative_id = current_proposal["initiative_id"]
    board = current_proposal["board"]
    expected_version = current_proposal["expected_version"]
    if target_kind == "initiative":
        card = conn.execute(
            "SELECT board_slug, record_version, closed_at "
            "FROM adrian_kanban_cards WHERE initiative_id = ? AND task_id IS NULL",
            (initiative_id,),
        ).fetchone()
        if card is None or card["board_slug"] != board:
            raise ValueError("initiative not found in the declared board")
        if card["record_version"] != expected_version:
            raise ValueError("expected version does not match the current initiative")
        _validate_closure_fields(
            current_proposal["source"], current_proposal["destination"], card["closed_at"]
        )
    else:
        card = conn.execute(
            "SELECT c.initiative_id, c.board_slug, c.record_version, c.closed_at "
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
        _validate_closure_fields(
            current_proposal["source"], current_proposal["destination"], None
        )

    # Consume through the Write-Gate's own path, which enforces the exact
    # approved state, authorizer evidence, executor session, expiry, and
    # canonical digest.
    try:
        consume_approved(
            conn,
            KanbanInitiativeApprovalConsumption(
                approval_id=stored["approval_id"],
                request_id=stored["request_id"],
                operation=stored["operation"],
                initiative_id=stored["initiative_id"],
                proposed_creation_id=None,
                expected_version=stored["expected_version"],
                canonical_digest=canonical_digest,
                canonicalization_version=CANONICALIZATION_VERSION,
                authorizer_evidence=stored["authorizer_evidence"],
                session_id=executor_session_id,
                expires_at=stored["expires_at"],
                consumed_mutation_id=mutation_id,
                consumed_idempotency_ref=idempotency_key,
            ),
            now=now,
        )
    except KanbanApprovalRejected as exc:
        raise ValueError(str(exc)) from None

    return {
        "request_id": stored["request_id"],
        "approval_id": stored["approval_id"],
        "canonical_payload": stored["canonical_payload"],
        "canonical_digest": canonical_digest,
        "approval_evidence": stored["approval_evidence"],
    }

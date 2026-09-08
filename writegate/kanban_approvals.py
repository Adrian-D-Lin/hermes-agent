import hashlib
import json
import sqlite3

from dataclasses import dataclass
from typing import Any, Optional

APPROVAL_TYPE = "kanban_initiative_mutation"

CANONICALIZATION_VERSION = 1

_STATE_VALID = ("prepared", "approved", "cancelled", "expired", "consumed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS write_gate_kanban_approvals (
    approval_id TEXT PRIMARY KEY NOT NULL,
    approval_type TEXT NOT NULL CHECK (approval_type = 'kanban_initiative_mutation'),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'approved', 'cancelled', 'expired', 'consumed')),
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    initiative_id TEXT,
    proposed_creation_id TEXT,
    expected_version INTEGER NOT NULL CHECK (expected_version >= 0),
    canonical_digest TEXT NOT NULL,
    canonicalization_version INTEGER NOT NULL CHECK (canonicalization_version = 1),
    authorizer_evidence TEXT NOT NULL,
    requires_distinct_authorizer INTEGER NOT NULL DEFAULT 0 CHECK (requires_distinct_authorizer IN (0, 1)),
    session_id TEXT NOT NULL,
    prepared_at INTEGER NOT NULL,
    approved_at INTEGER,
    expires_at INTEGER NOT NULL,
    approval_evidence TEXT,
    cancellation_evidence TEXT,
    consumed_mutation_id TEXT,
    consumed_idempotency_ref TEXT,
    UNIQUE(request_id),
    CHECK ((initiative_id IS NULL) <> (proposed_creation_id IS NULL))
)
"""


class KanbanApprovalRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class KanbanInitiativeApprovalPreparation:
    approval_id: str
    request_id: str
    operation: str
    initiative_id: Optional[str]
    proposed_creation_id: Optional[str]
    expected_version: int
    canonical_digest: str
    session_id: str
    expires_at: int
    requires_distinct_authorizer: bool = False

    def __post_init__(self) -> None:
        for _name in ("approval_id", "request_id", "operation", "canonical_digest", "session_id"):
            _value = getattr(self, _name)
            if not isinstance(_value, str) or not _value:
                raise KanbanApprovalRejected(f"{_name} must be a non-empty string")
        if not isinstance(self.requires_distinct_authorizer, bool):
            raise KanbanApprovalRejected("requires_distinct_authorizer must be a boolean")
        if not isinstance(self.expected_version, int) or isinstance(self.expected_version, bool) \
                or self.expected_version < 0:
            raise KanbanApprovalRejected("expected_version must be a non-negative integer")
        if not isinstance(self.expires_at, int) or isinstance(self.expires_at, bool) \
                or self.expires_at <= 0:
            raise KanbanApprovalRejected("expires_at must be a positive integer timestamp")
        if self.initiative_id is not None and self.proposed_creation_id is not None:
            raise KanbanApprovalRejected(
                "provide exactly one of initiative_id or proposed_creation_id")
        if self.initiative_id is None and self.proposed_creation_id is None:
            raise KanbanApprovalRejected(
                "provide exactly one of initiative_id or proposed_creation_id")
        if self.initiative_id is not None:
            if not isinstance(self.initiative_id, str) or not self.initiative_id.strip():
                raise KanbanApprovalRejected("initiative_id must be a non-empty string")
        if self.proposed_creation_id is not None:
            if not isinstance(self.proposed_creation_id, str) or not self.proposed_creation_id.strip():
                raise KanbanApprovalRejected("proposed_creation_id must be a non-empty string")


@dataclass(frozen=True)
class KanbanInitiativeApprovalConsumption:
    approval_id: str
    request_id: str
    operation: str
    initiative_id: Optional[str]
    proposed_creation_id: Optional[str]
    expected_version: int
    canonical_digest: str
    canonicalization_version: int
    authorizer_evidence: str
    session_id: str
    expires_at: int
    consumed_mutation_id: str
    consumed_idempotency_ref: str

    def __post_init__(self) -> None:
        for _name in ("approval_id", "request_id", "operation", "canonical_digest",
                      "authorizer_evidence", "session_id", "consumed_mutation_id",
                      "consumed_idempotency_ref"):
            _value = getattr(self, _name)
            if not isinstance(_value, str) or not _value:
                raise KanbanApprovalRejected(f"{_name} must be a non-empty string")
        if self.canonicalization_version != CANONICALIZATION_VERSION:
            raise KanbanApprovalRejected("canonicalization_version is not supported")
        if not isinstance(self.expected_version, int) or isinstance(self.expected_version, bool) \
                or self.expected_version < 0:
            raise KanbanApprovalRejected("expected_version must be a non-negative integer")
        if not isinstance(self.expires_at, int) or isinstance(self.expires_at, bool) \
                or self.expires_at <= 0:
            raise KanbanApprovalRejected("expires_at must be a positive integer timestamp")
        if self.initiative_id is not None and self.proposed_creation_id is not None:
            raise KanbanApprovalRejected(
                "provide exactly one of initiative_id or proposed_creation_id")
        if self.initiative_id is None and self.proposed_creation_id is None:
            raise KanbanApprovalRejected(
                "provide exactly one of initiative_id or proposed_creation_id")
        if self.initiative_id is not None:
            if not isinstance(self.initiative_id, str) or not self.initiative_id.strip():
                raise KanbanApprovalRejected("initiative_id must be a non-empty string")
        if self.proposed_creation_id is not None:
            if not isinstance(self.proposed_creation_id, str) or not self.proposed_creation_id.strip():
                raise KanbanApprovalRejected("proposed_creation_id must be a non-empty string")


def _resolve_authorizer_evidence(authorizer: Any) -> str:
    try:
        from gateway.trusted_authorizer_evidence import TrustedAuthorizerEvidence
    except Exception:
        raise KanbanApprovalRejected("trusted authorizer evidence is unavailable")
    if not isinstance(authorizer, TrustedAuthorizerEvidence):
        raise KanbanApprovalRejected("untrusted authorizer evidence")
    try:
        _evidence = authorizer._canonical_for_writegate()
    except Exception:
        raise KanbanApprovalRejected("authorizer evidence is invalid")
    if not isinstance(_evidence, str) or not _evidence:
        raise KanbanApprovalRejected("authorizer evidence is empty")
    return _evidence


class KanbanInitiativeApprovalHost:
    """Host-authority owner of kanban initiative approval lifecycle."""

    def __init__(self, authorizer: Any) -> None:
        self._authorizer = authorizer
        self._authorizer_evidence = _resolve_authorizer_evidence(authorizer)

    def prepare(self, conn: sqlite3.Connection, preparation: KanbanInitiativeApprovalPreparation,
                *, now: Optional[int] = None) -> None:
        if now is None:
            raise KanbanApprovalRejected("now timestamp is required")
        if not isinstance(now, int) or isinstance(now, bool) or now <= 0:
            raise KanbanApprovalRejected("now must be a positive integer timestamp")
        if preparation.expires_at <= now:
            raise KanbanApprovalRejected("expires_at must be greater than now")
        _values = (
            preparation.approval_id,
            APPROVAL_TYPE,
            "prepared",
            preparation.request_id,
            preparation.operation,
            preparation.initiative_id,
            preparation.proposed_creation_id,
            preparation.expected_version,
            preparation.canonical_digest,
            CANONICALIZATION_VERSION,
            self._authorizer_evidence,
            int(preparation.requires_distinct_authorizer),
            preparation.session_id,
            now,
            None,
            preparation.expires_at,
            None,
            None,
            None,
            None,
        )
        _cursor = conn.execute(
            "INSERT INTO write_gate_kanban_approvals "
            "(approval_id, approval_type, state, request_id, operation, initiative_id, "
            " proposed_creation_id, expected_version, canonical_digest, "
            " canonicalization_version, authorizer_evidence, requires_distinct_authorizer, "
            " session_id, prepared_at, "
            " approved_at, expires_at, approval_evidence, cancellation_evidence, "
            " consumed_mutation_id, consumed_idempotency_ref) "
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _values,
        )
        if _cursor.rowcount != 1:
            raise KanbanApprovalRejected("prepare did not create exactly one approval")

    def approve(self, conn: sqlite3.Connection, approval_id: str, approval_evidence: str,
                *, now: Optional[int] = None) -> None:
        if now is None:
            raise KanbanApprovalRejected("now timestamp is required")
        if not isinstance(now, int) or isinstance(now, bool) or now <= 0:
            raise KanbanApprovalRejected("now must be a positive integer timestamp")
        if not isinstance(approval_id, str) or not approval_id:
            raise KanbanApprovalRejected("approval_id must be a non-empty string")
        if not isinstance(approval_evidence, str) or not approval_evidence:
            raise KanbanApprovalRejected("approval_evidence must be a non-empty string")
        _cursor = conn.execute(
            "UPDATE write_gate_kanban_approvals SET state = 'approved', approved_at = ?, "
            " approval_evidence = ? "
            " WHERE approval_id = ? AND state = 'prepared' "
            " AND approval_type = ? AND authorizer_evidence = ? AND expires_at > ? "
            " AND requires_distinct_authorizer = 0",
            (now, approval_evidence, approval_id, APPROVAL_TYPE, self._authorizer_evidence, now),
        )
        if _cursor.rowcount != 1:
            raise KanbanApprovalRejected("approve did not update exactly one approval")

    def cancel(self, conn: sqlite3.Connection, approval_id: str,
               cancellation_evidence: str, *, now: Optional[int] = None) -> None:
        if now is None:
            raise KanbanApprovalRejected("now timestamp is required")
        if not isinstance(now, int) or isinstance(now, bool) or now <= 0:
            raise KanbanApprovalRejected("now must be a positive integer timestamp")
        if not isinstance(approval_id, str) or not approval_id:
            raise KanbanApprovalRejected("approval_id must be a non-empty string")
        if not isinstance(cancellation_evidence, str) or not cancellation_evidence:
            raise KanbanApprovalRejected("cancellation_evidence must be a non-empty string")
        _cursor = conn.execute(
            "UPDATE write_gate_kanban_approvals SET state = 'cancelled', "
            " cancellation_evidence = ? "
            " WHERE approval_id = ? AND approval_type = ? AND authorizer_evidence = ? "
            " AND state IN ('prepared', 'approved')",
            (cancellation_evidence, approval_id, APPROVAL_TYPE, self._authorizer_evidence),
        )
        if _cursor.rowcount != 1:
            raise KanbanApprovalRejected("cancel did not update exactly one approval")

    def approve_distinct(self, conn: sqlite3.Connection, approval_id: str, second_authorizer: Any, *,
                         expected_request_id: str, expected_canonical_digest: str,
                         approval_quote: str, now: Optional[int] = None) -> None:
        if now is None:
            raise KanbanApprovalRejected("now timestamp is required")
        if not isinstance(now, int) or isinstance(now, bool) or now <= 0:
            raise KanbanApprovalRejected("now must be a positive integer timestamp")
        if not isinstance(approval_id, str) or not approval_id:
            raise KanbanApprovalRejected("approval_id must be a non-empty string")
        if not isinstance(expected_request_id, str) or not expected_request_id:
            raise KanbanApprovalRejected("expected_request_id must be a non-empty string")
        if not isinstance(expected_canonical_digest, str) or not expected_canonical_digest:
            raise KanbanApprovalRejected("expected_canonical_digest must be a non-empty string")
        if not isinstance(approval_quote, str) or not approval_quote.strip():
            raise KanbanApprovalRejected("approval_quote must be a nonblank string")
        second_canonical = _resolve_authorizer_evidence(second_authorizer)
        try:
            second_payload = json.loads(second_canonical)
        except ValueError:
            raise KanbanApprovalRejected("second authorizer evidence is invalid")
        if not isinstance(second_payload, dict):
            raise KanbanApprovalRejected("second authorizer evidence is invalid")
        issued_at = second_payload.get("issued_at")
        expires_at = second_payload.get("expires_at")
        second_request_id = second_payload.get("request_id")
        if not isinstance(issued_at, int) or isinstance(issued_at, bool):
            raise KanbanApprovalRejected("second authorizer evidence time range is invalid")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise KanbanApprovalRejected("second authorizer evidence time range is invalid")
        if not (issued_at <= now < expires_at):
            raise KanbanApprovalRejected("second authorizer evidence is outside its valid time range")
        if not isinstance(second_request_id, str) or not second_request_id:
            raise KanbanApprovalRejected("second authorizer event request_id is missing")
        try:
            first_payload = json.loads(self._authorizer_evidence)
        except ValueError:
            raise KanbanApprovalRejected("first authorizer evidence is invalid")
        first_request_id = first_payload.get("request_id") if isinstance(first_payload, dict) else None
        if second_request_id == first_request_id:
            raise KanbanApprovalRejected("second authorizer event must use a distinct request_id")
        _receipt = {
            "second_authorizer": second_payload,
            "request_id": expected_request_id,
            "canonical_digest": expected_canonical_digest,
            "approval_quote": approval_quote,
            "approval_quote_sha256": hashlib.sha256(approval_quote.encode("utf-8")).hexdigest(),
        }
        _receipt_json = json.dumps(_receipt, sort_keys=True, separators=(",", ":"))
        _cursor = conn.execute(
            "UPDATE write_gate_kanban_approvals SET state = 'approved', approved_at = ?, "
            " approval_evidence = ? "
            " WHERE approval_id = ? AND state = 'prepared' "
            " AND approval_type = ? AND requires_distinct_authorizer = 1 "
            " AND authorizer_evidence = ? AND request_id = ? AND canonical_digest = ? "
            " AND expires_at > ?",
            (
                now,
                _receipt_json,
                approval_id,
                APPROVAL_TYPE,
                self._authorizer_evidence,
                expected_request_id,
                expected_canonical_digest,
                now,
            ),
        )
        if _cursor.rowcount != 1:
            raise KanbanApprovalRejected("approve_distinct did not update exactly one approval")


def consume_approved(conn: sqlite3.Connection, exact_request: KanbanInitiativeApprovalConsumption,
                     *, now: Optional[int] = None) -> None:
    if now is None:
        raise KanbanApprovalRejected("now timestamp is required")
    if not isinstance(now, int) or isinstance(now, bool) or now <= 0:
        raise KanbanApprovalRejected("now must be a positive integer timestamp")
    _cursor = conn.execute(
        "UPDATE write_gate_kanban_approvals SET state = 'consumed', "
        " consumed_mutation_id = ?, consumed_idempotency_ref = ? "
        " WHERE approval_id = ? AND approval_type = ? AND state = 'approved' "
        " AND request_id = ? AND operation = ? AND initiative_id IS ? "
        " AND proposed_creation_id IS ? AND expected_version = ? "
        " AND canonical_digest = ? AND canonicalization_version = ? "
        " AND authorizer_evidence = ? AND session_id = ? AND expires_at = ? AND expires_at > ?",
        (
            exact_request.consumed_mutation_id,
            exact_request.consumed_idempotency_ref,
            exact_request.approval_id,
            APPROVAL_TYPE,
            exact_request.request_id,
            exact_request.operation,
            exact_request.initiative_id,
            exact_request.proposed_creation_id,
            exact_request.expected_version,
            exact_request.canonical_digest,
            exact_request.canonicalization_version,
            exact_request.authorizer_evidence,
            exact_request.session_id,
            exact_request.expires_at,
            now,
        ),
    )
    if _cursor.rowcount != 1:
        raise KanbanApprovalRejected("consume did not update exactly one approval")


def create_kanban_approval_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the kanban approval table owned by this module."""
    conn.execute(_SCHEMA)
    _columns = {row[1] for row in conn.execute("PRAGMA table_info(write_gate_kanban_approvals)")}
    if "requires_distinct_authorizer" not in _columns:
        conn.execute(
            "ALTER TABLE write_gate_kanban_approvals "
            "ADD COLUMN requires_distinct_authorizer INTEGER NOT NULL DEFAULT 0 "
            "CHECK (requires_distinct_authorizer IN (0, 1))"
        )


__all__ = (
    "KanbanApprovalRejected",
    "KanbanInitiativeApprovalPreparation",
    "KanbanInitiativeApprovalConsumption",
    "KanbanInitiativeApprovalHost",
    "APPROVAL_TYPE",
    "create_kanban_approval_schema",
    "consume_approved",
)

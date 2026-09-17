"""Host-owned WriteGate approval flow for public initiative mutations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Callable

from gateway.trusted_authorizer_evidence import mint_current_tailscale_authorizer
from tools.approval_writegate import request_write_gate_approval
from writegate.kanban_approvals import (
    KanbanInitiativeApprovalHost,
    KanbanInitiativeApprovalPreparation,
    create_kanban_approval_schema,
)

from .schema import create_schema


def _nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_OPERATION_PRESENTATION = {
    "kanban_update_initiative": ("kanban-update", "update"),
    "kanban_transition_initiative": ("kanban-transition", "transition"),
}


class InitiativeMutationApprovalCoordinator:
    """Prepare, present, and persist one exact initiative-mutation approval."""

    def __init__(
        self,
        database_path: str,
        *,
        now_provider: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        database_path = _nonblank(database_path, "database_path")
        if not os.path.isabs(database_path):
            raise ValueError("database_path must be absolute")
        if not callable(now_provider):
            raise TypeError("now_provider must be callable")
        self._database_path = os.path.abspath(database_path)
        self._now_provider = now_provider

    def authorize(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        session_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        try:
            request_prefix, operation_label = _OPERATION_PRESENTATION[operation]
        except (KeyError, TypeError):
            raise ValueError("unsupported initiative mutation operation") from None
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        if "approval_id" in payload:
            raise ValueError("payload must not contain approval_id before preparation")
        session_id = _nonblank(session_id, "session_id")
        turn_id = _nonblank(turn_id, "turn_id")
        initiative_id = _nonblank(payload.get("initiative_id"), "initiative_id")
        board = _nonblank(payload.get("board"), "board")
        digest = _canonical_digest(payload)
        request_id = f"{request_prefix}:{turn_id}:{digest[:16]}"
        approval_id = f"{request_id}:approval"
        now = self._now()

        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        try:
            create_schema(conn)
            create_kanban_approval_schema(conn)
            conn.commit()
            row = self._approval_row(conn, request_id)
            host = None
            if row is None:
                expected_version = self._initiative_version(
                    conn,
                    initiative_id=initiative_id,
                    board=board,
                )
                authorizer = mint_current_tailscale_authorizer(
                    request_id=request_id,
                    issued_at=now,
                    ttl_seconds=300,
                )
                host = KanbanInitiativeApprovalHost(authorizer)
                host.prepare(
                    conn,
                    KanbanInitiativeApprovalPreparation(
                        approval_id=approval_id,
                        request_id=request_id,
                        operation=operation,
                        initiative_id=initiative_id,
                        proposed_creation_id=None,
                        expected_version=expected_version,
                        canonical_digest=digest,
                        session_id=session_id,
                        expires_at=now + 300,
                    ),
                    now=now,
                )
                conn.commit()
                row = self._approval_row(conn, request_id)

            self._validate_row(
                row,
                approval_id=approval_id,
                operation=operation,
                initiative_id=initiative_id,
                digest=digest,
                session_id=session_id,
            )
            state = row["state"]
            if state in {"cancelled", "expired"} or row["expires_at"] <= now:
                return {
                    "approved": False,
                    "state": "cancelled",
                    "request_id": request_id,
                    "approval_id": approval_id,
                }
            if state == "prepared":
                if host is None:
                    host = self._host_for_existing(row)
                decision = request_write_gate_approval(
                    request_id=request_id,
                    command=json.dumps(payload, indent=2, sort_keys=True),
                    description=(
                        f"Apply this exact Initiative Tracker {operation_label}. No initiative "
                        "state changes unless this approval is accepted once."
                    ),
                    session_key=session_id,
                    timeout_seconds=300,
                )
                decision_now = self._now()
                if not (
                    isinstance(decision, dict)
                    and decision.get("approved") is True
                    and decision.get("decision") == "once"
                ):
                    host.cancel(
                        conn,
                        approval_id,
                        json.dumps(
                            {"decision": "deny", "result": decision},
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        now=decision_now,
                    )
                    conn.commit()
                    return {
                        "approved": False,
                        "state": "cancelled",
                        "request_id": request_id,
                        "approval_id": approval_id,
                    }
                host.approve(
                    conn,
                    approval_id,
                    json.dumps(
                        {
                            "decision": "once",
                            "approval_reference": str(
                                decision.get("approval_reference") or request_id
                            ),
                            "decision_at": decision.get("decision_at"),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now=decision_now,
                )
                conn.commit()
                state = "approved"
            elif state not in {"approved", "consumed"}:
                raise ValueError(f"unsupported initiative approval state {state!r}")

            return {
                "approved": True,
                "state": state,
                "request_id": request_id,
                "approval_id": approval_id,
            }
        finally:
            conn.close()

    def _now(self) -> int:
        now = self._now_provider()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("now_provider must return a positive integer")
        return now

    @staticmethod
    def _initiative_version(
        conn: sqlite3.Connection,
        *,
        initiative_id: str,
        board: str,
    ) -> int:
        rows = conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND board_slug = ? "
            "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
            (initiative_id, board),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"expected exactly one open initiative in board {board!r}, "
                f"found {len(rows)}"
            )
        version = rows[0][0]
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("initiative record_version must be nonnegative")
        return version

    @staticmethod
    def _approval_row(conn: sqlite3.Connection, request_id: str) -> Any:
        return conn.execute(
            "SELECT approval_id, state, request_id, operation, initiative_id, "
            "proposed_creation_id, expected_version, canonical_digest, "
            "authorizer_evidence, requires_distinct_authorizer, session_id, "
            "expires_at FROM write_gate_kanban_approvals WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    @staticmethod
    def _validate_row(
        row: Any,
        *,
        approval_id: str,
        operation: str,
        initiative_id: str,
        digest: str,
        session_id: str,
    ) -> None:
        if row is None:
            raise ValueError("initiative update approval was not persisted")
        expected = {
            "approval_id": approval_id,
            "operation": operation,
            "initiative_id": initiative_id,
            "proposed_creation_id": None,
            "canonical_digest": digest,
            "requires_distinct_authorizer": 0,
            "session_id": session_id,
        }
        for field, value in expected.items():
            if row[field] != value:
                raise ValueError(
                    f"initiative mutation approval {field} does not match proposal"
                )

    @staticmethod
    def _host_for_existing(row: Any) -> KanbanInitiativeApprovalHost:
        try:
            evidence = json.loads(row["authorizer_evidence"])
            issued_at = evidence["issued_at"]
            expires_at = evidence["expires_at"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stored authorizer evidence is invalid") from exc
        authorizer = mint_current_tailscale_authorizer(
            request_id=row["request_id"],
            issued_at=issued_at,
            ttl_seconds=expires_at - issued_at,
        )
        if authorizer._canonical_for_writegate() != row["authorizer_evidence"]:
            raise ValueError(
                "initiative mutation approval belongs to a different authenticated "
                "transport; submit a fresh mutation request"
            )
        return KanbanInitiativeApprovalHost(authorizer)


__all__ = ["InitiativeMutationApprovalCoordinator"]

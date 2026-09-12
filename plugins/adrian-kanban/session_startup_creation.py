"""WriteGate-governed initiative creation for Session Startup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict

from gateway.trusted_authorizer_evidence import mint_current_tailscale_authorizer
from tools.approval import request_write_gate_approval
from writegate.kanban_approvals import (
    KanbanInitiativeApprovalHost,
    KanbanInitiativeApprovalPreparation,
    create_kanban_approval_schema,
)

from .schema import create_schema


_PROPOSAL_FIELDS = {
    "title",
    "objective",
    "initiative_id",
    "body",
    "request_id",
    "approval_id",
}


def _nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def _canonical_digest(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class SessionStartupInitiativeCreationCoordinator:
    """Derive, approve and execute one existing initiative-create command."""

    def __init__(
        self,
        database_path: str,
        command_boundary: Any,
        *,
        now_provider: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        database_path = _nonblank(database_path, "database_path")
        if not os.path.isabs(database_path):
            raise ValueError("database_path must be absolute")
        if not callable(getattr(command_boundary, "submit", None)):
            raise TypeError("command_boundary must provide submit")
        if not callable(now_provider):
            raise TypeError("now_provider must be callable")
        self._database_path = os.path.abspath(database_path)
        self._command_boundary = command_boundary
        self._now_provider = now_provider

    def derive(
        self,
        project: Dict[str, Any],
        title: str,
        objective: str,
        record: Dict[str, Any],
    ) -> Dict[str, str]:
        project_id, project_name, board, primary_path = self._project(project)
        _nonblank(record.get("session_id") if isinstance(record, dict) else None,
                  "record.session_id")
        title = _nonblank(title, "title")
        objective = _nonblank(objective, "objective")

        slug = re.sub(r"[^a-z0-9]+", "_", title.casefold()).strip("_")[:40]
        slug = slug.rstrip("_") or "initiative"
        identity_input = [project_id, board, title, objective]
        suffix = hashlib.sha256(
            json.dumps(
                identity_input,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:8]
        initiative_id = f"i_{slug}_{suffix}"
        request_id = f"session-startup-create-request-{uuid.uuid4().hex}"
        approval_id = f"session-startup-create-approval-{uuid.uuid4().hex}"
        body = "\n".join(
            (
                "# [[INITIATIVE_LEDGER]]",
                "## Initiative",
                f"- Initiative ID: {initiative_id}",
                f"- Title: {title}",
                "### Objective",
                objective,
                "### Board and workspace context",
                f"- Project: {project_name} ({project_id})",
                f"- Board: {board}",
                f"- Primary repository: {primary_path}",
                "### Authoritative artifacts",
                "None recorded.",
                "### Cleared outcomes",
                "None recorded.",
                "### Open items",
                "None recorded.",
                "### Related task cards",
                "None recorded.",
                "### Constraints",
                "None recorded.",
                "### Cold-session continuation",
                (
                    f"Select initiative {initiative_id} through Session Startup. "
                    "Its initial lifecycle position is D1."
                ),
            )
        )
        return {
            "title": title,
            "objective": objective,
            "initiative_id": initiative_id,
            "body": body,
            "request_id": request_id,
            "approval_id": approval_id,
        }

    def execute(
        self,
        project: Dict[str, Any],
        draft: Dict[str, Any],
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        _, _, board, _ = self._project(project)
        draft = self._draft(draft)
        session_id = _nonblank(
            record.get("session_id") if isinstance(record, dict) else None,
            "record.session_id",
        )
        payload = {
            "initiative_id": draft["initiative_id"],
            "title": draft["title"],
            "body": draft["body"],
            "board": board,
        }
        digest = _canonical_digest(payload)
        now = self._now()

        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        try:
            create_schema(conn)
            create_kanban_approval_schema(conn)
            conn.commit()
            row = self._approval_row(conn, draft["approval_id"])
            host = None
            if row is None:
                authorizer = mint_current_tailscale_authorizer(
                    request_id=draft["request_id"],
                    issued_at=now,
                    ttl_seconds=300,
                )
                host = KanbanInitiativeApprovalHost(authorizer)
                host.prepare(
                    conn,
                    KanbanInitiativeApprovalPreparation(
                        approval_id=draft["approval_id"],
                        request_id=draft["request_id"],
                        operation="kanban_create_initiative",
                        initiative_id=None,
                        proposed_creation_id=draft["initiative_id"],
                        expected_version=0,
                        canonical_digest=digest,
                        session_id=session_id,
                        expires_at=now + 300,
                    ),
                    now=now,
                )
                conn.commit()
                row = self._approval_row(conn, draft["approval_id"])
            self._validate_approval_row(row, draft, digest, session_id)

            state = row["state"]
            if state in {"cancelled", "expired"} or row["expires_at"] <= now:
                return {
                    "status": "cancelled",
                    "message": "Initiative creation was cancelled or expired.",
                }
            if state == "prepared":
                if host is None:
                    host = self._host_for_existing(row)
                decision = request_write_gate_approval(
                    request_id=draft["request_id"],
                    command=json.dumps(payload, indent=2, sort_keys=True),
                    description=(
                        "Create this complete Initiative Tracker record in "
                        f"project {project['name']}."
                    ),
                    session_key=session_id,
                    timeout_seconds=300,
                )
                decision_now = self._now()
                if not (
                    isinstance(decision, dict)
                    and decision.get("approved") is True
                    and decision.get("decision") == "once"
                    and isinstance(decision.get("decision_at"), str)
                    and decision["decision_at"].strip()
                ):
                    host.cancel(
                        conn,
                        draft["approval_id"],
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
                        "status": "cancelled",
                        "message": "Initiative creation was not approved.",
                    }
                host.approve(
                    conn,
                    draft["approval_id"],
                    json.dumps(
                        {
                            "decision": "once",
                            "approval_reference": str(
                                decision.get("approval_reference")
                                or draft["request_id"]
                            ),
                            "decision_at": decision["decision_at"],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now=decision_now,
                )
                conn.commit()
            elif state not in {"approved", "consumed"}:
                raise ValueError(f"unsupported creation approval state {state!r}")
        finally:
            conn.close()

        command_payload = dict(payload)
        command_payload["approval_id"] = draft["approval_id"]
        execution_id = f"{draft['request_id']}:execute"
        result = self._command_boundary.submit(
            "kanban_create_initiative",
            attempt_id=draft["request_id"],
            idempotency_key=execution_id,
            target=draft["initiative_id"],
            expected_version=0,
            session_id=session_id,
            workspace_id=None,
            execution_context="session-startup",
            actor_profile="default",
            payload=command_payload,
        )
        if not isinstance(result, dict) or result.get("result") != "ACCEPTED":
            raise ValueError(f"initiative command was rejected: {result!r}")
        value = result.get("value")
        if not isinstance(value, dict):
            raise ValueError("initiative command returned no value")
        if value.get("initiative_id") != draft["initiative_id"]:
            raise ValueError("initiative command returned a different identity")
        if value.get("phase") != "D1":
            raise ValueError("initiative command did not initialize phase D1")
        return {
            "status": "created",
            "initiative": {
                "initiative_id": draft["initiative_id"],
                "title": draft["title"],
                "current_phase": "D1",
                "current_segment_id": None,
                "closed_at": None,
            },
        }

    def cancel(
        self,
        project: Dict[str, Any],
        draft: Dict[str, Any],
        record: Dict[str, Any],
    ) -> None:
        self._project(project)
        draft = self._draft(draft)
        _nonblank(
            record.get("session_id") if isinstance(record, dict) else None,
            "record.session_id",
        )
        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        try:
            create_schema(conn)
            create_kanban_approval_schema(conn)
            conn.commit()
            row = self._approval_row(conn, draft["approval_id"])
            if row is None or row["state"] not in {"prepared", "approved"}:
                return
            try:
                host = self._host_for_existing(row)
            except Exception:
                return
            host.cancel(
                conn,
                draft["approval_id"],
                json.dumps(
                    {"decision": "cancelled-by-session-startup"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                now=self._now(),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _project(project: Any) -> tuple[str, str, str, str]:
        if not isinstance(project, dict):
            raise ValueError("project must be an object")
        return tuple(
            _nonblank(project.get(field), f"project.{field}")
            for field in ("id", "name", "board_slug", "primary_path")
        )

    @staticmethod
    def _draft(draft: Any) -> Dict[str, str]:
        if not isinstance(draft, dict) or set(draft) != _PROPOSAL_FIELDS:
            raise ValueError("creation draft has an invalid field set")
        return {field: _nonblank(draft[field], f"draft.{field}") for field in draft}

    def _now(self) -> int:
        now = self._now_provider()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("now_provider must return a positive integer")
        return now

    @staticmethod
    def _approval_row(conn: sqlite3.Connection, approval_id: str) -> Any:
        return conn.execute(
            "SELECT approval_id, state, request_id, operation, initiative_id, "
            "proposed_creation_id, expected_version, canonical_digest, "
            "authorizer_evidence, session_id, expires_at "
            "FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()

    @staticmethod
    def _validate_approval_row(
        row: Any,
        draft: Dict[str, str],
        digest: str,
        session_id: str,
    ) -> None:
        if row is None:
            raise ValueError("creation approval was not persisted")
        expected = {
            "approval_id": draft["approval_id"],
            "request_id": draft["request_id"],
            "operation": "kanban_create_initiative",
            "initiative_id": None,
            "proposed_creation_id": draft["initiative_id"],
            "expected_version": 0,
            "canonical_digest": digest,
            "session_id": session_id,
        }
        for field, value in expected.items():
            if row[field] != value:
                raise ValueError(f"creation approval {field} does not match proposal")

    @staticmethod
    def _host_for_existing(row: Any) -> KanbanInitiativeApprovalHost:
        try:
            evidence = json.loads(row["authorizer_evidence"])
            issued_at = evidence["issued_at"]
            expires_at = evidence["expires_at"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stored authorizer evidence is invalid") from exc
        ttl = expires_at - issued_at
        authorizer = mint_current_tailscale_authorizer(
            request_id=row["request_id"],
            issued_at=issued_at,
            ttl_seconds=ttl,
        )
        if authorizer._canonical_for_writegate() != row["authorizer_evidence"]:
            raise ValueError(
                "creation approval belongs to a different authenticated transport; "
                "select Change to replace the proposal"
            )
        return KanbanInitiativeApprovalHost(authorizer)


__all__ = ["SessionStartupInitiativeCreationCoordinator"]

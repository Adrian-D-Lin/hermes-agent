import hashlib
import json
import sqlite3
import time

from tools.approval import request_write_gate_approval
from gateway.trusted_authorizer_evidence import mint_current_tailscale_authorizer
from writegate.kanban_approvals import KanbanInitiativeApprovalHost

__all__ = ["present_and_record_override_approval"]


def present_and_record_override_approval(database_path, *, initial_authorizer, prepared, session_key, now):
    if type(database_path) is not str or not database_path.strip():
        raise ValueError("database_path must be a nonblank string")
    if type(session_key) is not str or not session_key.strip():
        raise ValueError("session_key must be a nonblank string")
    if type(now) is not int or now <= 0:
        raise ValueError("now must be a positive integer")
    if type(prepared) is not dict:
        raise ValueError("prepared must be a dict")
    required_keys = {"request_id", "approval_id", "canonical_payload", "canonical_digest"}
    if set(prepared.keys()) != required_keys:
        raise ValueError("prepared must have exactly request_id, approval_id, canonical_payload, canonical_digest")
    for key in required_keys:
        val = prepared[key]
        if type(val) is not str or not val.strip():
            raise ValueError(f"prepared[{key}] must be a nonblank string")

    request_id = prepared["request_id"]
    approval_id = prepared["approval_id"]
    canonical_payload = prepared["canonical_payload"]
    canonical_digest = prepared["canonical_digest"]

    # Verify database state before presentation
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT p.request_id, p.approval_id, p.canonical_payload, p.canonical_digest,
                   a.state, a.request_id AS a_request_id, a.canonical_digest AS a_digest,
                   a.approval_id AS a_approval_id, a.expires_at
            FROM gate_override_proposals p
            JOIN write_gate_kanban_approvals a ON a.approval_id = p.approval_id
            WHERE p.request_id = ?
            """,
            (request_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise ValueError("Expected exactly one joined row")
        row = rows[0]
        if row["approval_id"] != approval_id:
            raise ValueError("approval_id mismatch")
        if row["canonical_payload"] != canonical_payload:
            raise ValueError("canonical_payload mismatch")
        if row["canonical_digest"] != canonical_digest:
            raise ValueError("canonical_digest mismatch")
        recomputed = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        if recomputed != canonical_digest:
            raise ValueError("SHA256 digest mismatch")
        if row["state"] != "prepared":
            raise ValueError("state mismatch")
        if row["a_request_id"] != request_id:
            raise ValueError("a.request_id mismatch")
        if row["a_digest"] != canonical_digest:
            raise ValueError("a.digest mismatch")
        if row["a_approval_id"] != approval_id:
            raise ValueError("a.approval_id mismatch")
        if row["expires_at"] <= now:
            raise ValueError("approval expired")
    finally:
        conn.close()

    # Construct presentation request_id
    presentation_request_id = f"kanban-gate-override:{request_id}:{canonical_digest}"

    # Build description containing exact request_id, digest, and canonical_payload
    description = (
        f"Kanban Gate Override Approval\n"
        f"Request ID: {request_id}\n"
        f"Digest: {canonical_digest}\n"
        f"Payload: {canonical_payload}"
    )
    command = "Approve Kanban Gate Override"

    # Call write gate approval
    try:
        result = request_write_gate_approval(
            request_id=presentation_request_id,
            command=command,
            description=description,
            session_key=session_key,
            timeout_seconds=300,
        )
    except Exception:
        result = None

    # Check approval
    approved = False
    approval_reference = presentation_request_id
    decision = "deny"
    if isinstance(result, dict):
        if result.get("approved") is True and result.get("decision") == "once":
            ref = result.get("approval_reference")
            if isinstance(ref, str) and ref.strip():
                approved = True
                approval_reference = ref
        dec = result.get("decision")
        if isinstance(dec, str) and dec.strip():
            decision = dec
        ref = result.get("approval_reference")
        if isinstance(ref, str) and ref.strip():
            approval_reference = ref

    if not approved:
        # Non-approval path: cancel
        fresh_now = int(time.time())
        conn = sqlite3.connect(database_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            host = KanbanInitiativeApprovalHost(initial_authorizer)
            evidence = json.dumps(
                {"decision": decision, "approval_reference": approval_reference},
                separators=(",", ":"),
            )
            host.cancel(conn, approval_id, evidence, now=fresh_now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"approved": False, "request_id": request_id, "canonical_digest": canonical_digest}

    # Approval path: mint second evidence
    fresh_now = int(time.time())
    second_evidence = mint_current_tailscale_authorizer(
        request_id=approval_reference,
        issued_at=fresh_now,
        ttl_seconds=120,
    )

    # Record distinct approval
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        host = KanbanInitiativeApprovalHost(initial_authorizer)
        approval_quote = json.dumps(
            {
                "decision": "once",
                "approval_reference": approval_reference,
                "request_id": request_id,
                "canonical_digest": canonical_digest,
            },
            separators=(",", ":"),
        )
        host.approve_distinct(
            conn,
            approval_id,
            second_evidence,
            expected_request_id=request_id,
            expected_canonical_digest=canonical_digest,
            approval_quote=approval_quote,
            now=fresh_now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "approved": True,
        "approval_reference": approval_reference,
        "request_id": request_id,
        "canonical_digest": canonical_digest,
    }

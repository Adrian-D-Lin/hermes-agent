"""Ratified two-interaction approval contract; no live ingress or model mocks."""

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from gateway import trusted_authorizer_evidence as trusted
from writegate.kanban_approvals import (
    KanbanApprovalRejected,
    KanbanInitiativeApprovalHost,
    create_kanban_approval_schema,
    consume_approved,
)
from tests.test_adrian_kanban_s2 import _preparation, _consumption


def evidence(request_id, issued_at=1001):
    connection = trusted._record_authenticated_tailscale_connection(
        "gateway/tailscale", "same-connection", "adrian@tailnet", request_id, 1000
    )
    return trusted.TrustedAuthorizerEvidence._from_authenticated_connection(
        connection, issued_at=issued_at, ttl_seconds=120
    )


@pytest.fixture
def approval_case():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    create_kanban_approval_schema(conn)
    first = evidence("initial-instruction")
    host = KanbanInitiativeApprovalHost(first)
    preparation = replace(_preparation(), requires_distinct_authorizer=True)
    host.prepare(conn, preparation, now=1010)
    yield conn, host, first
    conn.close()


def approve(conn, host, second=None, **changes):
    args = dict(
        expected_request_id="request-1",
        expected_canonical_digest="sha256:payload",
        approval_quote="Approve this exact proposal",
        now=1020,
    )
    args.update(changes)
    host.approve_distinct(
        conn,
        "approval-1",
        evidence("second-approval", 1011) if second is None else second,
        **args,
    )


def row(conn):
    return conn.execute(
        "SELECT * FROM write_gate_kanban_approvals WHERE approval_id='approval-1'"
    ).fetchone()


def test_distinct_actions_may_share_connection_peer_and_session(approval_case):
    conn, host, first = approval_case
    assert row(conn)["requires_distinct_authorizer"] == 1
    approve(conn, host)
    saved = row(conn)
    assert saved["state"] == "approved"
    assert saved["authorizer_evidence"] == first._canonical_for_writegate()
    receipt = json.loads(saved["approval_evidence"])
    assert receipt["second_authorizer"]["request_id"] == "second-approval"
    assert receipt["request_id"] == "request-1"
    assert receipt["canonical_digest"] == "sha256:payload"
    assert receipt["approval_quote"] == "Approve this exact proposal"
    assert (
        receipt["approval_quote_sha256"]
        == hashlib.sha256(b"Approve this exact proposal").hexdigest()
    )
    with pytest.raises(KanbanApprovalRejected):
        approve(conn, host)


@pytest.mark.parametrize(
    "gap",
    [
        "same_action_later_time",
        "expired_evidence",
        "future_evidence",
        "forged_dict",
        "wrong_digest",
        "wrong_request",
        "expired_proposal",
        "cancelled",
        "blank_quote",
        "legacy_approve",
    ],
)
def test_invalid_second_action_cannot_approve_or_change_pending_record(
    approval_case, gap
):
    conn, host, _ = approval_case
    second = evidence("second-approval", 1011)
    changes = {}
    if gap == "same_action_later_time":
        second = evidence("initial-instruction", 1012)
    elif gap == "expired_evidence":
        changes["now"] = 1131
    elif gap == "future_evidence":
        second = evidence("second-approval", 1050)
    elif gap == "forged_dict":
        second = json.loads(second._canonical_for_writegate())
    elif gap == "wrong_digest":
        changes["expected_canonical_digest"] = "sha256:different-preview"
    elif gap == "wrong_request":
        changes["expected_request_id"] = "another-preview"
    elif gap == "expired_proposal":
        changes["now"] = 1200
        second = evidence("second-approval", 1190)
    elif gap == "cancelled":
        host.cancel(conn, "approval-1", "cancel", now=1015)
    elif gap == "blank_quote":
        changes["approval_quote"] = " "
    before = dict(row(conn))
    with pytest.raises(KanbanApprovalRejected):
        if gap == "legacy_approve":
            host.approve(conn, "approval-1", "arbitrary approval string", now=1020)
        else:
            approve(conn, host, second, **changes)
    assert dict(row(conn)) == before


def test_distinct_approval_and_consumption_obey_outer_rollback(approval_case):
    conn, host, first = approval_case
    conn.execute("BEGIN IMMEDIATE")
    approve(conn, host)
    consume_approved(conn, _consumption(first._canonical_for_writegate()), now=1030)
    assert row(conn)["state"] == "consumed"
    conn.rollback()
    assert row(conn)["state"] == "prepared"
    assert row(conn)["approval_evidence"] is None


def test_approval_receipt_preserves_exact_unicode_and_whitespace(approval_case):
    conn, host, _ = approval_case
    quote = "  Approved — keep the unmet criterion visible.\n"
    approve(conn, host, approval_quote=quote)
    receipt = json.loads(row(conn)["approval_evidence"])
    assert receipt["approval_quote"] == quote
    assert (
        receipt["approval_quote_sha256"]
        == hashlib.sha256(quote.encode("utf-8")).hexdigest()
    )


def test_distinct_approval_supports_default_sqlite_row_factory(approval_case):
    conn, host, _ = approval_case
    conn.row_factory = None
    approve(conn, host)
    conn.row_factory = sqlite3.Row
    assert row(conn)["state"] == "approved"


@pytest.mark.parametrize("invalid_now", [None, True, 0, -1, "1020", 1020.5])
def test_invalid_clock_cannot_approve_pending_request(approval_case, invalid_now):
    conn, host, _ = approval_case
    before = dict(row(conn))
    with pytest.raises(KanbanApprovalRejected):
        approve(conn, host, now=invalid_now)
    assert dict(row(conn)) == before


def test_distinct_approval_cannot_be_applied_to_an_ordinary_preparation(approval_case):
    conn, host, _ = approval_case
    conn.execute(
        "UPDATE write_gate_kanban_approvals SET requires_distinct_authorizer=0"
    )
    before = dict(row(conn))
    with pytest.raises(KanbanApprovalRejected):
        approve(conn, host)
    assert dict(row(conn)) == before


def test_other_first_authorizer_cannot_approve_this_proposal(approval_case):
    conn, _, _ = approval_case
    other_host = KanbanInitiativeApprovalHost(evidence("different-initial-action"))
    before = dict(row(conn))
    with pytest.raises(KanbanApprovalRejected):
        approve(conn, other_host)
    assert dict(row(conn)) == before


def test_unknown_proposal_cannot_create_an_approval(approval_case):
    conn, host, _ = approval_case
    conn.execute("DELETE FROM write_gate_kanban_approvals")
    with pytest.raises(KanbanApprovalRejected):
        approve(conn, host)
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("invalid", [None, "true", 1, 0])
def test_distinct_requirement_is_an_exact_boolean(invalid):
    with pytest.raises(KanbanApprovalRejected):
        replace(_preparation(), requires_distinct_authorizer=invalid)


def test_schema_upgrade_preserves_existing_ordinary_approval():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE write_gate_kanban_approvals (
        approval_id TEXT PRIMARY KEY, approval_type TEXT, state TEXT, request_id TEXT, operation TEXT,
        initiative_id TEXT, proposed_creation_id TEXT, expected_version INTEGER, canonical_digest TEXT,
        canonicalization_version INTEGER, authorizer_evidence TEXT, session_id TEXT, prepared_at INTEGER,
        approved_at INTEGER, expires_at INTEGER, approval_evidence TEXT, cancellation_evidence TEXT,
        consumed_mutation_id TEXT, consumed_idempotency_ref TEXT)""")
    conn.execute(
        "INSERT INTO write_gate_kanban_approvals (approval_id,state,canonical_digest) VALUES ('legacy','approved','old-digest')"
    )
    conn.commit()
    create_kanban_approval_schema(conn)
    create_kanban_approval_schema(conn)
    saved = conn.execute(
        "SELECT * FROM write_gate_kanban_approvals WHERE approval_id='legacy'"
    ).fetchone()
    assert (
        saved["state"],
        saved["canonical_digest"],
        saved["requires_distinct_authorizer"],
    ) == ("approved", "old-digest", 0)
    conn.close()

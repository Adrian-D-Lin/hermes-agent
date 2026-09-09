"""Host-visible second approval for an immutable Kanban override proposal."""

from __future__ import annotations

import importlib
import json
import sqlite3
import time

import pytest

from tests.test_adrian_kanban_s3_initiatives import commands_module  # noqa: F401
from tests.test_adrian_kanban_s3_override_approval import evidence
from tests.test_adrian_kanban_s3_override_proposals import arguments
from writegate.kanban_approvals import create_kanban_approval_schema


@pytest.fixture
def host_case(commands_module, tmp_path, monkeypatch):
    schema = importlib.import_module(f"{commands_module.__package__}.schema")
    proposals = importlib.import_module(
        f"{commands_module.__package__}.override_proposals"
    )
    flow = importlib.import_module(f"{commands_module.__package__}.override_host_flow")
    path = tmp_path / "kanban.sqlite3"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    schema.create_schema(conn)
    create_kanban_approval_schema(conn)
    conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
    conn.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type,initiative_id,title,created_at,board_slug) "
        "VALUES ('initiative','initiative-1','Initiative',1000,'orchestrator')"
    )
    args = arguments()
    conn.execute("BEGIN IMMEDIATE")
    prepared = proposals.store_prepared_override(conn, **args)
    conn.commit()
    conn.close()
    return path, flow, args, prepared, monkeypatch


def _approval(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone())
    finally:
        conn.close()


def test_exact_visible_once_approval_becomes_distinct_durable_approval(host_case):
    path, flow, args, prepared, monkeypatch = host_case
    shown = {}
    monkeypatch.setattr(time, "time", lambda: 1020)

    def present(**request):
        shown.update(request)
        return {
            "approved": True,
            "decision": "once",
            "approval_reference": "host-click-1",
            "decision_at": "2026-09-09T01:02:03+00:00",
        }

    monkeypatch.setattr(flow, "request_write_gate_approval", present)
    monkeypatch.setattr(
        flow,
        "mint_current_tailscale_authorizer",
        lambda **kwargs: evidence(kwargs["request_id"], kwargs["issued_at"]),
    )

    result = flow.present_and_record_override_approval(
        str(path),
        initial_authorizer=args["initial_authorizer"],
        prepared=prepared,
        session_key="human-session",
        now=1020,
    )

    assert result == {
        "approved": True,
        "approval_reference": "host-click-1",
        "request_id": prepared["request_id"],
        "canonical_digest": prepared["canonical_digest"],
    }
    assert shown["request_id"].startswith("kanban-gate-override:")
    assert prepared["request_id"] in shown["description"]
    assert prepared["canonical_digest"] in shown["description"]
    assert prepared["canonical_payload"] in shown["description"]
    saved = _approval(path)
    assert saved["state"] == "approved"
    receipt = json.loads(saved["approval_evidence"])
    assert receipt["second_authorizer"]["request_id"] == "host-click-1"
    assert receipt["request_id"] == prepared["request_id"]
    assert receipt["canonical_digest"] == prepared["canonical_digest"]


def test_second_approval_uses_fresh_post_dialogue_timestamp(host_case):
    path, flow, args, prepared, monkeypatch = host_case
    observed = {}

    def present(**_request):
        return {
            "approved": True,
            "decision": "once",
            "approval_reference": "host-click-later",
        }

    monkeypatch.setattr(flow, "request_write_gate_approval", present)
    monkeypatch.setattr(time, "time", lambda: 1080)

    def mint(**kwargs):
        observed.update(kwargs)
        return evidence(kwargs["request_id"], kwargs["issued_at"])

    monkeypatch.setattr(flow, "mint_current_tailscale_authorizer", mint)

    flow.present_and_record_override_approval(
        str(path),
        initial_authorizer=args["initial_authorizer"],
        prepared=prepared,
        session_key="human-session",
        now=1020,
    )

    assert observed["issued_at"] == 1080
    assert _approval(path)["approved_at"] == 1080


@pytest.mark.parametrize("decision", ["deny", "timeout", None])
def test_nonapproval_cancels_without_mutating_the_card(host_case, decision):
    path, flow, args, prepared, monkeypatch = host_case
    before = sqlite3.connect(path).execute(
        "SELECT record_version,closed_at FROM adrian_kanban_cards"
    ).fetchone()
    monkeypatch.setattr(
        flow,
        "request_write_gate_approval",
        lambda **_: {
            "approved": False,
            "decision": decision,
            "approval_reference": "host-denial-1",
            "decision_at": "",
        },
    )

    result = flow.present_and_record_override_approval(
        str(path),
        initial_authorizer=args["initial_authorizer"],
        prepared=prepared,
        session_key="human-session",
        now=1020,
    )

    assert result["approved"] is False
    approval = _approval(path)
    assert approval["state"] == "cancelled"
    cancellation = json.loads(approval["cancellation_evidence"])
    assert cancellation["decision"] == (
        decision if isinstance(decision, str) and decision.strip() else "deny"
    )
    assert cancellation["approval_reference"] == "host-denial-1"
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT record_version,closed_at FROM adrian_kanban_cards"
        ).fetchone() == before
        assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "wrong"},
        {"canonical_digest": "wrong"},
        {"canonical_payload": "{}"},
        {"approval_id": "wrong"},
    ],
)
def test_tampered_prepared_result_is_rejected_before_presentation(host_case, change):
    path, flow, args, prepared, monkeypatch = host_case
    calls = []
    monkeypatch.setattr(flow, "request_write_gate_approval", lambda **kw: calls.append(kw))
    altered = dict(prepared)
    altered.update(change)

    with pytest.raises(ValueError):
        flow.present_and_record_override_approval(
            str(path),
            initial_authorizer=args["initial_authorizer"],
            prepared=altered,
            session_key="human-session",
            now=1020,
        )

    assert calls == []
    assert _approval(path)["state"] == "prepared"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_path", str),
        ("session_key", str),
        ("now", True),
        ("prepared", dict),
    ],
)
def test_host_inputs_require_exact_concrete_types(host_case, field, value):
    path, flow, args, prepared, _ = host_case
    kwargs = {
        "database_path": str(path),
        "initial_authorizer": args["initial_authorizer"],
        "prepared": prepared,
        "session_key": "human-session",
        "now": 1020,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        flow.present_and_record_override_approval(**kwargs)

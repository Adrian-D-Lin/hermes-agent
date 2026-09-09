"""Preparation seam from authoritative initiative state into Write-Gate storage."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiatives import (
    _database,
    _seed_initiative,
    _seed_reconciliation,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_override_approval import evidence


@pytest.fixture
def preparation_case(commands_module, tmp_path, monkeypatch):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    _seed_reconciliation(
        path,
        result_id="reconciliation-1",
        from_phase="D1",
        to_phase="DEV1",
    )
    module = importlib.import_module(
        f"{commands_module.__package__}.initiative_override"
    )
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn, module
    conn.close()


def _prepare(conn, module, **changes):
    args = {
        "initial_authorizer": evidence("turn-1"),
        "initial_session_id": "human-session-1",
        "initial_message_id": "turn-1",
        "initial_quote": (
            "Prepare an override moving initiative-1 to DEV1; do not move it "
            "until I approve the exact proposal."
        ),
        "executor_session_id": "human-session-1",
        "executor_profile": "default",
        "initiative_id": "initiative-1",
        "board": "orchestrator",
        "to_phase": "DEV1",
        "to_segment_id": None,
        "reconciliation_ref": "reconciliation-1",
        "override_reason": "Adrian explicitly directed this nonstandard route.",
        "now": 1010,
        "expires_at": 1310,
    }
    args.update(changes)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = module.prepare_initiative_transition_override(conn, **args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
        return result


def test_preparation_derives_and_stores_one_exact_unmutated_proposal(preparation_case):
    conn, module = preparation_case
    before_card = tuple(
        conn.execute(
            "SELECT * FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()
    )

    prepared = _prepare(conn, module)

    assert set(prepared) == {
        "request_id",
        "approval_id",
        "canonical_payload",
        "canonical_digest",
    }
    assert prepared["request_id"] == "kanban-gate-override:turn-1"
    assert prepared["approval_id"] == "kanban-gate-override-approval:turn-1"
    stored = conn.execute("SELECT * FROM gate_override_proposals").fetchone()
    approval = conn.execute("SELECT * FROM write_gate_kanban_approvals").fetchone()
    payload = json.loads(stored["canonical_payload"])
    assert payload["proposal"]["destination"] == {
        "phase": "DEV1",
        "segment_id": None,
        "closed_at": None,
    }
    assert payload["proposal"]["gates"][0]["result"] == "unmet"
    assert payload["initial_instruction"]["message_id"] == "turn-1"
    assert payload["initial_instruction"]["session_id"] == "human-session-1"
    assert payload["executor"] == {
        "session_id": "human-session-1",
        "profile": "default",
    }
    assert approval["state"] == "prepared"
    assert approval["request_id"] == prepared["request_id"]
    assert tuple(
        conn.execute(
            "SELECT * FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()
    ) == before_card
    assert (
        conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"initial_message_id": "other-turn"}, "request_id"),
        ({"executor_session_id": "other-session"}, "session"),
        ({"executor_profile": "builder-tester"}, "default"),
        ({"expires_at": 1010}, "expires_at"),
        ({"override_reason": " "}, "override_reason"),
    ],
)
def test_invalid_preparation_is_atomic(preparation_case, change, match):
    conn, module = preparation_case
    with pytest.raises(ValueError, match=match):
        _prepare(conn, module, **change)
    assert conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 0
    )


def test_one_initial_turn_cannot_prepare_twice(preparation_case):
    conn, module = preparation_case
    _prepare(conn, module)
    with pytest.raises(ValueError):
        _prepare(conn, module)
    assert conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0]
        == 1
    )

"""Live derivation of an initiative transition gate-override proposal."""

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


@pytest.fixture
def derive_case(commands_module, tmp_path, monkeypatch):
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
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn, module
    conn.close()


def _derive(conn, module, **changes):
    args = {
        "initiative_id": "initiative-1",
        "board": "orchestrator",
        "to_phase": "DEV1",
        "to_segment_id": None,
        "reconciliation_ref": "reconciliation-1",
        "override_reason": "Adrian explicitly directed the nonstandard move.",
        "executor_session_id": "session-1",
        "executor_profile": "default",
    }
    args.update(changes)
    return module.derive_initiative_transition_override(conn, **args)


def test_derivation_preserves_unmet_ordinary_gate_and_exact_live_position(derive_case):
    _, conn, module = derive_case

    proposal = _derive(conn, module)

    assert set(proposal) == {
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
    assert proposal["operation"] == "kanban_execute_initiative_gate_override"
    assert proposal["target"] == proposal["initiative_id"] == "initiative-1"
    assert proposal["target_kind"] == "initiative"
    assert proposal["expected_version"] == 0
    assert proposal["source"] == {
        "phase": "D1",
        "segment_id": None,
        "predecessor_id": 1,
        "closed_at": None,
    }
    assert proposal["destination"] == {
        "phase": "DEV1",
        "segment_id": None,
        "closed_at": None,
    }
    assert proposal["gates"] == [
        {
            "code": "phase_close",
            "result": "unmet",
            "evidence_ref": None,
            "observed": "no accepted phase_close authorizes this exact transition",
        }
    ]
    assert all(
        check["result"] == "met" for check in proposal["non_bypassable_checks"]
    )
    assert proposal["claim_run_closures"] == []
    assert proposal["dependency_changes"] == []
    assert proposal["downstream_exceptions"] == []
    assert proposal["commentary"] == (
        "Per Adrian's explicit instruction, initiative-1 moved from D1 to DEV1; "
        "gate criteria overridden, not satisfied."
    )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"reconciliation_ref": "missing"}, "reconciliation"),
        ({"executor_profile": "builder-tester"}, "default"),
        ({"to_phase": "unknown"}, "phase"),
        ({"to_phase": "D3", "to_segment_id": "S1"}, "segment"),
        ({"override_reason": " "}, "override_reason"),
    ],
)
def test_nonbypassable_or_shape_failure_produces_no_proposal(derive_case, change, match):
    _, conn, module = derive_case
    with pytest.raises((ValueError, module.CommandRejected), match=match):
        _derive(conn, module, **change)


def test_closed_initiative_derives_one_combined_reopen_and_move(derive_case):
    _, conn, module = derive_case
    conn.execute(
        "UPDATE adrian_kanban_cards SET closed_at=1015 WHERE initiative_id='initiative-1'"
    )
    conn.commit()

    proposal = _derive(conn, module)

    assert proposal["source"]["closed_at"] == 1015
    assert proposal["destination"]["closed_at"] is None


def test_derivation_is_read_only(derive_case):
    _, conn, module = derive_case
    before = {
        "card": tuple(conn.execute("SELECT * FROM adrian_kanban_cards").fetchone()),
        "transitions": conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0],
        "approvals": conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0],
        "proposals": conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0],
    }
    _derive(conn, module)
    after = {
        "card": tuple(conn.execute("SELECT * FROM adrian_kanban_cards").fetchone()),
        "transitions": conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0],
        "approvals": conn.execute("SELECT COUNT(*) FROM write_gate_kanban_approvals").fetchone()[0],
        "proposals": conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0],
    }
    assert after == before


def test_derivation_records_an_existing_exact_phase_close_as_met(derive_case):
    path, conn, module = derive_case
    reconciliation_payload = {
        "initiative_id": "initiative-1",
        "board": "orchestrator",
        "previous_transition_id": 1,
        "from_phase": "D1",
        "from_segment_id": None,
        "to_phase": "D2",
        "to_segment_id": None,
        "canon_route": "Canon/design-lifecycle.md#D2",
        "exit_gate_ref": "exit-gate:D1:D2",
        "verification_result": "accepted",
    }
    conn.execute(
        "UPDATE initiative_phase_results SET result_id='reconciliation-d2', "
        "canonical_payload=?, idempotency_key='key-reconciliation-d2' "
        "WHERE result_id='reconciliation-1'",
        (json.dumps(reconciliation_payload, sort_keys=True, separators=(",", ":")),),
    )
    card_id = conn.execute(
        "SELECT id FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO initiative_phase_results "
        "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
        "iteration, result_kind, contract_id, contract_version, canonical_payload, "
        "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
        "idempotency_key, accepted, created_at) VALUES "
        "('close-d1', ?, 'initiative-1', 'D1', NULL, 1, 'phase_close', "
        "'adrian-kanban.lifecycle.d1', '1', ?, '[]', '[]', ?, "
        "'close-d1-key', 1, 3)",
        (
            card_id,
            json.dumps({"next_route": "D2"}),
            json.dumps({"source_transition_id": 1, "actor_profile": "default"}),
        ),
    )
    conn.commit()

    proposal = _derive(
        conn,
        module,
        to_phase="D2",
        reconciliation_ref="reconciliation-d2",
    )

    assert proposal["gates"] == [
        {
            "code": "phase_close",
            "result": "met",
            "evidence_ref": "close-d1",
            "observed": "accepted phase_close authorizes this exact transition",
        }
    ]

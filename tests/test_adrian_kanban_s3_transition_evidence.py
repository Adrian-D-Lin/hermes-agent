"""Ordinary transition evidence: ratified v0.28 section 7.12, not overrides."""

import importlib
import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiatives import (
    commands_module,  # noqa: F401
    _database,
    _seed_initiative,
)


@pytest.fixture
def evidence_case(commands_module, tmp_path, monkeypatch):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    with sqlite3.connect(path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE card_type='initiative'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, iteration, result_kind, "
            "contract_id, contract_version, canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) "
            "VALUES ('close-1', ?, 'initiative-1', 'D1', NULL, 1, 'phase_close', "
            "'adrian-kanban.lifecycle.d1', '1', ?, '[]', '[]', ?, 'close-key', 1, 2)",
            (
                card_id,
                json.dumps({"next_route": "D2"}),
                json.dumps({"source_transition_id": 1, "actor_profile": "default"}),
            ),
        )
    module = importlib.import_module(
        f"{commands_module.__package__}.transition_evidence"
    )
    arguments = dict(
        initiative_card_id=card_id,
        initiative_id="initiative-1",
        from_phase="D1",
        from_segment_id=None,
        to_phase="D2",
        to_segment_id=None,
        previous_transition_id=1,
        phase_close_ref="close-1",
    )
    return path, module, arguments


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "missing",
        "unaccepted",
        "wrong_card",
        "wrong_initiative",
        "wrong_phase",
        "wrong_segment",
        "wrong_kind",
        "wrong_contract",
        "wrong_version",
        "wrong_route",
        "malformed_result",
        "stale_visit",
        "missing_visit",
        "boolean_visit",
        "malformed_actor",
        "malformed_task_refs",
        "duplicate_task_refs",
        "dangling_task",
        "dangling_checkpoint",
        "blank_checkpoint",
        "wrong_current_phase",
        "wrong_predecessor",
        "destination_segment",
        "unsupported_phase",
    ],
)
def test_ordinary_transition_requires_exact_current_admitted_evidence(
    evidence_case, gap
):
    path, module, arguments = evidence_case
    column_values = {
        "unaccepted": ("accepted", 0),
        "wrong_phase": ("phase", "D2"),
        "wrong_segment": ("segment_id", "S1"),
        "wrong_kind": ("result_kind", "repository_reconciliation"),
        "wrong_contract": ("contract_id", "other"),
        "wrong_version": ("contract_version", "99"),
        "wrong_route": ("canonical_payload", '{"next_route":"D3"}'),
        "malformed_result": ("canonical_payload", "[]"),
        "stale_visit": ("actor_evidence", '{"source_transition_id":2}'),
        "missing_visit": ("actor_evidence", "{}"),
        "boolean_visit": ("actor_evidence", '{"source_transition_id":true}'),
        "malformed_actor": ("actor_evidence", "not-json"),
        "malformed_task_refs": ("accepted_task_refs", "{}"),
        "duplicate_task_refs": ("accepted_task_refs", '["candidate","candidate"]'),
        "dangling_task": ("accepted_task_refs", '["missing-candidate"]'),
        "dangling_checkpoint": ("accepted_checkpoint_refs", '["missing-checkpoint"]'),
        "blank_checkpoint": ("accepted_checkpoint_refs", '[" "]'),
    }
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if gap in column_values:
            column, value = column_values[gap]
            conn.execute(
                f"UPDATE initiative_phase_results SET {column}=? WHERE result_id='close-1'",
                (value,),
            )
        elif gap == "missing":
            arguments["phase_close_ref"] = "missing"
        elif gap == "wrong_card":
            arguments["initiative_card_id"] += 1
        elif gap == "wrong_initiative":
            arguments["initiative_id"] = "another"
        elif gap == "wrong_current_phase":
            conn.execute("UPDATE initiative_transitions SET to_phase='D2'")
        elif gap == "wrong_predecessor":
            arguments["previous_transition_id"] = 2
        elif gap == "destination_segment":
            arguments["to_segment_id"] = "S1"
        elif gap == "unsupported_phase":
            arguments["from_phase"] = "DEV1"
        before = conn.total_changes
        if gap is None:
            assert module.validate_transition_evidence(conn, **arguments) == {
                "next_route": "D2"
            }
        else:
            with pytest.raises(ValueError):
                module.validate_transition_evidence(conn, **arguments)
        assert conn.total_changes == before


def test_not_dry_return_uses_the_admitted_route_without_inferred_forward_only_order(
    evidence_case,
):
    path, module, arguments = evidence_case
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE initiative_transitions SET to_phase='D2'")
        conn.execute(
            "UPDATE initiative_phase_results SET phase='D2', contract_id='adrian-kanban.lifecycle.d2', canonical_payload=?",
            (json.dumps({"conclusion": "NOT_DRY", "next_route": "D1"}),),
        )
        arguments.update(from_phase="D2", to_phase="D1")
        assert (
            module.validate_transition_evidence(conn, **arguments)["next_route"] == "D1"
        )


def test_completed_d4_can_enter_dev1_without_requiring_dev1_to_be_completed(
    evidence_case,
):
    path, module, arguments = evidence_case
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE initiative_transitions SET to_phase='D4'")
        conn.execute(
            "UPDATE initiative_phase_results SET phase='D4', contract_id='adrian-kanban.lifecycle.d4', canonical_payload=?",
            (json.dumps({"next_route": "DEV1"}),),
        )
        arguments.update(from_phase="D4", to_phase="DEV1")
        assert (
            module.validate_transition_evidence(conn, **arguments)["next_route"]
            == "DEV1"
        )

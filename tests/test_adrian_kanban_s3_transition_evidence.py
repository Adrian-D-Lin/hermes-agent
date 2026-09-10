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


@pytest.fixture
def dev1_segment_evidence_case(commands_module, tmp_path, monkeypatch):
    """A DEV1.7 result is the admitted DEV1 exit result, not phase_close."""
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    definitions = [
        {"segment_id": "S1", "ordinal": 1, "dependency_ids": []},
        {"segment_id": "S2", "ordinal": 2, "dependency_ids": ["S1"]},
    ]
    with sqlite3.connect(path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE card_type='initiative'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE initiative_transitions SET to_phase='DEV1' "
            "WHERE initiative_id='initiative-1'"
        )
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES "
            "('projection-1', 1, ?, 'initiative-1', 'segments.json', ?, ?, ?, ?, "
            "'accepted', 2)",
            (
                card_id,
                "a" * 40,
                "b" * 64,
                json.dumps(definitions),
                json.dumps({"S1": "ready:S1", "S2": "ready:S2"}),
            ),
        )
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
            "idempotency_key, accepted, created_at) VALUES "
            "('dev1-close', ?, 'initiative-1', 'DEV1', NULL, 1, "
            "'segment_manifest_projection', 'adrian-kanban.lifecycle.dev1', '1', "
            "?, '[]', '[]', ?, 'dev1-close-key', 1, 2)",
            (
                card_id,
                json.dumps(
                    {
                        "projection_id": "projection-1",
                        "projection_version": 1,
                        "readiness_refs": {"S1": "ready:S1", "S2": "ready:S2"},
                    }
                ),
                json.dumps(
                    {"source_transition_id": 1, "actor_profile": "default"}
                ),
            ),
        )
    module = importlib.import_module(
        f"{commands_module.__package__}.transition_evidence"
    )
    return path, module, dict(
        initiative_card_id=card_id,
        initiative_id="initiative-1",
        from_phase="DEV1",
        from_segment_id=None,
        to_phase="DEV2",
        to_segment_id="S1",
        previous_transition_id=1,
        phase_close_ref="dev1-close",
    )


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "wrong_route",
        "not_first_segment",
        "stale_visit",
        "wrong_projection",
        "incomplete_readiness",
        "wrong_kind",
        "source_segment",
    ],
)
def test_dev1_to_first_segment_uses_exact_combined_dev1_7_result(
    dev1_segment_evidence_case, gap
):
    path, module, arguments = dev1_segment_evidence_case
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if gap == "wrong_route":
            arguments["to_phase"] = "DEV3"
        elif gap == "not_first_segment":
            arguments["to_segment_id"] = "S2"
        elif gap == "stale_visit":
            conn.execute(
                "UPDATE initiative_phase_results SET actor_evidence=? "
                "WHERE result_id='dev1-close'",
                (json.dumps({"source_transition_id": 99, "actor_profile": "default"}),),
            )
        elif gap == "wrong_projection":
            payload = json.loads(
                conn.execute(
                    "SELECT canonical_payload FROM initiative_phase_results "
                    "WHERE result_id='dev1-close'"
                ).fetchone()[0]
            )
            payload["projection_id"] = "other"
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? "
                "WHERE result_id='dev1-close'",
                (json.dumps(payload),),
            )
        elif gap == "incomplete_readiness":
            payload = json.loads(
                conn.execute(
                    "SELECT canonical_payload FROM initiative_phase_results "
                    "WHERE result_id='dev1-close'"
                ).fetchone()[0]
            )
            payload["readiness_refs"].pop("S2")
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? "
                "WHERE result_id='dev1-close'",
                (json.dumps(payload),),
            )
        elif gap == "wrong_kind":
            conn.execute(
                "UPDATE initiative_phase_results SET result_kind='phase_close' "
                "WHERE result_id='dev1-close'"
            )
        elif gap == "source_segment":
            arguments["from_segment_id"] = "S1"

        before = conn.total_changes
        if gap is None:
            result = module.validate_transition_evidence(conn, **arguments)
            assert result["projection_id"] == "projection-1"
        else:
            with pytest.raises(ValueError):
                module.validate_transition_evidence(conn, **arguments)
        assert conn.total_changes == before


@pytest.fixture
def dev4_next_segment_evidence_case(commands_module, tmp_path, monkeypatch):
    """An accepted DEV4.5 checkpoint alone admits the exact next segment."""
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    with sqlite3.connect(path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE card_type='initiative'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE initiative_transitions SET to_phase='DEV4', to_segment_id='S1' "
            "WHERE initiative_id='initiative-1'"
        )
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
            "idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:DEV4.5:S1', ?, 'initiative-1', 'DEV4', 'S1', 7, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev4', '1', "
            "?, '[]', '[\"checkpoint:DEV4.4:S1\"]', ?, "
            "'dev4-5-key', 1, 2)",
            (
                card_id,
                json.dumps(
                    {
                        "step": "DEV4.5",
                        "workspace_retirement_checkpoint_ref": "checkpoint:DEV4.4:S1",
                        "completed_segment_id": "S1",
                        "action": "admit_next_segment",
                        "next_segment_id": "S2",
                        "closure_evidence_ref": "closure:S1@" + "a" * 40,
                        "next_route": "DEV2",
                    }
                ),
                json.dumps(
                    {"source_transition_id": 1, "actor_profile": "default"}
                ),
            ),
        )
    module = importlib.import_module(
        f"{commands_module.__package__}.transition_evidence"
    )
    return path, module, dict(
        initiative_card_id=card_id,
        initiative_id="initiative-1",
        from_phase="DEV4",
        from_segment_id="S1",
        to_phase="DEV2",
        to_segment_id="S2",
        previous_transition_id=1,
        phase_close_ref="checkpoint:DEV4.5:S1",
    )


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "missing_ref",
        "unaccepted",
        "wrong_segment",
        "wrong_step",
        "wrong_action",
        "wrong_completed_segment",
        "wrong_next_segment",
        "wrong_route",
        "stale_visit",
        "source_segment_missing",
        "destination_phase",
        "destination_segment_missing",
    ],
)
def test_dev4_5_admits_only_its_exact_next_segment_transition(
    dev4_next_segment_evidence_case, gap
):
    path, module, arguments = dev4_next_segment_evidence_case
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if gap == "missing_ref":
            arguments["phase_close_ref"] = "missing"
        elif gap == "unaccepted":
            conn.execute(
                "UPDATE initiative_phase_results SET accepted=0 "
                "WHERE result_id='checkpoint:DEV4.5:S1'"
            )
        elif gap == "wrong_segment":
            conn.execute(
                "UPDATE initiative_phase_results SET segment_id='S2' "
                "WHERE result_id='checkpoint:DEV4.5:S1'"
            )
        elif gap == "stale_visit":
            conn.execute(
                "UPDATE initiative_phase_results SET actor_evidence=? "
                "WHERE result_id='checkpoint:DEV4.5:S1'",
                (json.dumps({"source_transition_id": 99, "actor_profile": "default"}),),
            )
        elif gap == "source_segment_missing":
            arguments["from_segment_id"] = None
        elif gap == "destination_phase":
            arguments["to_phase"] = "DEV3"
        elif gap == "destination_segment_missing":
            arguments["to_segment_id"] = None
        else:
            field_values = {
                "wrong_step": ("step", "DEV4.4"),
                "wrong_action": ("action", "close_initiative"),
                "wrong_completed_segment": ("completed_segment_id", "S0"),
                "wrong_next_segment": ("next_segment_id", "S3"),
                "wrong_route": ("next_route", "CLOSED"),
            }
            if gap in field_values:
                payload = json.loads(
                    conn.execute(
                        "SELECT canonical_payload FROM initiative_phase_results "
                        "WHERE result_id='checkpoint:DEV4.5:S1'"
                    ).fetchone()[0]
                )
                field, value = field_values[gap]
                payload[field] = value
                conn.execute(
                    "UPDATE initiative_phase_results SET canonical_payload=? "
                    "WHERE result_id='checkpoint:DEV4.5:S1'",
                    (json.dumps(payload),),
                )

        before = conn.total_changes
        if gap is None:
            result = module.validate_transition_evidence(conn, **arguments)
            assert result["action"] == "admit_next_segment"
            assert result["next_segment_id"] == "S2"
        else:
            with pytest.raises(ValueError):
                module.validate_transition_evidence(conn, **arguments)
        assert conn.total_changes == before

"""DEV2.3 convergence checkpoint and same-segment transition evidence."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_orchestration_checkpoints import (
    _approve,
    _boundary,
    _database,
    _seed_initiative,
    _submit,
    commands_module,  # noqa: F401
)


def _seed_dev2_pair(path, card_id, *, segment_id="S1", workspace_id="ws:S1"):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE initiative_transitions SET to_segment_id=? "
            "WHERE initiative_id='initiative-1'",
            (segment_id,),
        )
        for ordinal, (step, profile, metadata) in enumerate(
            (
                (
                    "DEV2.1",
                    "independent-reviewer",
                    {
                        "brief_ref": "2-design/S1/brief.md@" + "a" * 40,
                        "source_inventory": ["dispatch:S1"],
                        "file_change_map": [],
                        "test_plan": ["happy", "unhappy", "fringe"],
                        "exit_criteria": ["checked"],
                        "risks": [],
                    },
                ),
                (
                    "DEV2.2",
                    "test-authority-reviewer",
                    {"findings": [], "conclusion": "ACCEPTED"},
                ),
            ),
            start=1,
        ):
            task_id = f"task:{step}"
            candidate_id = f"candidate:{step}"
            task_card_id = conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, body, created_at, "
                "board_slug, record_version) VALUES "
                "('task', 'initiative-1', ?, ?, '', ?, 'orchestrator', 0)",
                (task_id, step, ordinal + 1),
            ).lastrowid
            contract_payload = {
                "predecessor_ref": "candidate:DEV2.1" if step == "DEV2.2" else None
            }
            conn.execute(
                "INSERT INTO task_lifecycle_contracts "
                "(contract_id, contract_version, step, task_card_id, task_id, "
                "initiative_card_id, initiative_id, segment_id, workspace_id, "
                "execution_profile, canonical_contract_payload, registry_hash, "
                "skill_id, skill_version, skill_hash, created_at) VALUES "
                "('adrian-kanban.lifecycle.dev2', '1', ?, ?, ?, ?, "
                "'initiative-1', ?, ?, ?, ?, ?, 'dev2-implementation-brief', "
                "'0.1.0', ?, ?)",
                (
                    step,
                    task_card_id,
                    task_id,
                    card_id,
                    segment_id,
                    workspace_id,
                    profile,
                    json.dumps(contract_payload),
                    "b" * 64,
                    "c" * 64,
                    ordinal + 1,
                ),
            )
            conn.execute(
                "INSERT INTO task_candidate_handoffs "
                "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
                "summary, metadata_json, submitted_by, created_at) VALUES "
                "(?, ?, ?, ?, 'test-authority-reviewer', '', ?, ?, ?)",
                (
                    candidate_id,
                    task_card_id,
                    task_id,
                    ordinal,
                    json.dumps(metadata),
                    profile,
                    ordinal + 1,
                ),
            )
            conn.execute(
                "INSERT INTO task_reviewer_verdicts "
                "(verdict_id, task_card_id, task_id, candidate_id, review_run_id, "
                "reviewer, verdict, summary, created_at) VALUES "
                "(?, ?, ?, ?, ?, 'test-authority-reviewer', 'accepted', '', ?)",
                (
                    f"verdict:{step}",
                    task_card_id,
                    task_id,
                    candidate_id,
                    100 + ordinal,
                    ordinal + 1,
                ),
            )
        conn.commit()


def _payload(**result_changes):
    result = {
        "step": "DEV2.3",
        "final_brief_candidate_ref": "candidate:DEV2.1",
        "final_check_candidate_ref": "candidate:DEV2.2",
        "final_brief_ref": "2-design/S1/brief.md@" + "a" * 40,
        "checked_brief_sha": "a" * 40,
        "handoff_package_ref": "2-design/S1/dev3-handoff.md@" + "d" * 40,
        "no_concealed_design_issue": True,
        "next_route": "DEV3",
    }
    result.update(result_changes)
    return {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "update": {
            "result_id": "checkpoint:DEV2.3:S1",
            "phase": "DEV2",
            "segment_id": "S1",
            "iteration": 1,
            "contract_id": "adrian-kanban.lifecycle.dev2",
            "contract_version": "1",
            "result": result,
            "accepted_task_refs": ["candidate:DEV2.1", "candidate:DEV2.2"],
            "accepted_checkpoint_refs": [],
        },
        "approval_id": "approval:DEV2.3:S1",
        "board": "orchestrator",
    }


def test_dev2_3_public_checkpoint_pins_final_clean_pair_and_current_visit(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV2")
    _seed_dev2_pair(path, card_id)
    payload = _payload()
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "ACCEPTED", response
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT phase, segment_id, result_kind, canonical_payload, "
            "accepted_task_refs, actor_evidence FROM initiative_phase_results "
            "WHERE result_id='checkpoint:DEV2.3:S1'"
        ).fetchone()
        assert tuple(row[:3]) == ("DEV2", "S1", "orchestration_checkpoint")
        assert json.loads(row["canonical_payload"])["next_route"] == "DEV3"
        assert json.loads(row["accepted_task_refs"]) == [
            "candidate:DEV2.1",
            "candidate:DEV2.2",
        ]
        assert json.loads(row["actor_evidence"])["source_transition_id"] == 1


@pytest.mark.parametrize(
    "gap",
    [
        "wrong_segment",
        "wrong_workspace",
        "wrong_profile",
        "wrong_predecessor",
        "brief_mismatch",
        "bad_sha",
        "failed_check",
        "check_findings",
        "concealed_design_issue",
        "wrong_route",
        "extra_result_field",
    ],
)
def test_dev2_3_rejects_nonconverged_or_cross_segment_evidence(
    commands_module, tmp_path, monkeypatch, gap
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV2")
    _seed_dev2_pair(path, card_id)
    payload = _payload()
    with sqlite3.connect(path) as conn:
        if gap == "wrong_segment":
            payload["update"]["segment_id"] = "S2"
        elif gap == "wrong_workspace":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET workspace_id='ws:other' "
                "WHERE step='DEV2.2'"
            )
        elif gap == "wrong_profile":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET execution_profile='default' "
                "WHERE step='DEV2.2'"
            )
        elif gap == "wrong_predecessor":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET canonical_contract_payload='{}' "
                "WHERE step='DEV2.2'"
            )
        elif gap == "brief_mismatch":
            payload["update"]["result"]["final_brief_ref"] = "other"
        elif gap == "bad_sha":
            payload["update"]["result"]["checked_brief_sha"] = "not-a-sha"
        elif gap == "failed_check":
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=? "
                "WHERE candidate_id='candidate:DEV2.2'",
                (json.dumps({"findings": [], "conclusion": "REQUIRES_CHANGES"}),),
            )
        elif gap == "check_findings":
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=? "
                "WHERE candidate_id='candidate:DEV2.2'",
                (
                    json.dumps(
                        {
                            "findings": [
                                {"citation": "design", "impact": "gap", "route": "DEV2.1"}
                            ],
                            "conclusion": "ACCEPTED",
                        }
                    ),
                ),
            )
        elif gap == "concealed_design_issue":
            payload["update"]["result"]["no_concealed_design_issue"] = False
        elif gap == "wrong_route":
            payload["update"]["result"]["next_route"] = "DEV4"
        elif gap == "extra_result_field":
            payload["update"]["result"]["extra"] = "invented"
        conn.commit()
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "REJECTED", response
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_phase_results"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "approved"


def test_dev2_to_dev3_transition_evidence_is_same_segment_and_current_visit(
    commands_module, tmp_path, monkeypatch
):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV2")
    _seed_dev2_pair(path, card_id)
    payload = _payload()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
            "idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:DEV2.3:S1', ?, 'initiative-1', 'DEV2', 'S1', 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev2', '1', "
            "?, ?, '[]', ?, 'checkpoint-key', 1, 4)",
            (
                card_id,
                json.dumps(payload["update"]["result"]),
                json.dumps(payload["update"]["accepted_task_refs"]),
                json.dumps({"actor_profile": "default", "source_transition_id": 1}),
            ),
        )
        module = importlib.import_module(
            f"{commands_module.__package__}.transition_evidence"
        )
        arguments = dict(
            initiative_card_id=card_id,
            initiative_id="initiative-1",
            from_phase="DEV2",
            from_segment_id="S1",
            to_phase="DEV3",
            to_segment_id="S1",
            previous_transition_id=1,
            phase_close_ref="checkpoint:DEV2.3:S1",
        )
        assert module.validate_transition_evidence(conn, **arguments)["step"] == "DEV2.3"
        for change in (
            {"to_segment_id": "S2"},
            {"to_phase": "DEV4"},
            {"previous_transition_id": 2},
        ):
            with pytest.raises(ValueError):
                module.validate_transition_evidence(conn, **(arguments | change))
